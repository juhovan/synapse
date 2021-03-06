# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import heapq
import itertools
import logging
from typing import Dict, List, Optional

from twisted.internet import defer

import synapse.state
from synapse import event_auth
from synapse.api.constants import EventTypes
from synapse.api.errors import AuthError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import EventBase
from synapse.types import StateMap
from synapse.util import Clock

logger = logging.getLogger(__name__)


# We want to yield to the reactor occasionally during state res when dealing
# with large data sets, so that we don't exhaust the reactor. This is done by
# yielding to reactor during loops every N iterations.
_YIELD_AFTER_ITERATIONS = 100


@defer.inlineCallbacks
def resolve_events_with_store(
    clock: Clock,
    room_id: str,
    room_version: str,
    state_sets: List[StateMap[str]],
    event_map: Optional[Dict[str, EventBase]],
    state_res_store: "synapse.state.StateResolutionStore",
):
    """Resolves the state using the v2 state resolution algorithm

    Args:
        clock
        room_id: the room we are working in
        room_version: The room version
        state_sets: List of dicts of (type, state_key) -> event_id,
            which are the different state groups to resolve.
        event_map:
            a dict from event_id to event, for any events that we happen to
            have in flight (eg, those currently being persisted). This will be
            used as a starting point fof finding the state we need; any missing
            events will be requested via state_res_store.

            If None, all events will be fetched via state_res_store.

        state_res_store:

    Returns:
        Deferred[dict[(str, str), str]]:
            a map from (type, state_key) to event_id.
    """

    logger.debug("Computing conflicted state")

    # We use event_map as a cache, so if its None we need to initialize it
    if event_map is None:
        event_map = {}

    # First split up the un/conflicted state
    unconflicted_state, conflicted_state = _seperate(state_sets)

    if not conflicted_state:
        return unconflicted_state

    logger.debug("%d conflicted state entries", len(conflicted_state))
    logger.debug("Calculating auth chain difference")

    # Also fetch all auth events that appear in only some of the state sets'
    # auth chains.
    auth_diff = yield _get_auth_chain_difference(state_sets, event_map, state_res_store)

    full_conflicted_set = set(
        itertools.chain(
            itertools.chain.from_iterable(conflicted_state.values()), auth_diff
        )
    )

    events = yield state_res_store.get_events(
        [eid for eid in full_conflicted_set if eid not in event_map],
        allow_rejected=True,
    )
    event_map.update(events)

    # everything in the event map should be in the right room
    for event in event_map.values():
        if event.room_id != room_id:
            raise Exception(
                "Attempting to state-resolve for room %s with event %s which is in %s"
                % (room_id, event.event_id, event.room_id,)
            )

    full_conflicted_set = {eid for eid in full_conflicted_set if eid in event_map}

    logger.debug("%d full_conflicted_set entries", len(full_conflicted_set))

    # Get and sort all the power events (kicks/bans/etc)
    power_events = (
        eid for eid in full_conflicted_set if _is_power_event(event_map[eid])
    )

    sorted_power_events = yield _reverse_topological_power_sort(
        clock, room_id, power_events, event_map, state_res_store, full_conflicted_set
    )

    logger.debug("sorted %d power events", len(sorted_power_events))

    # Now sequentially auth each one
    resolved_state = yield _iterative_auth_checks(
        clock,
        room_id,
        room_version,
        sorted_power_events,
        unconflicted_state,
        event_map,
        state_res_store,
    )

    logger.debug("resolved power events")

    # OK, so we've now resolved the power events. Now sort the remaining
    # events using the mainline of the resolved power level.

    set_power_events = set(sorted_power_events)
    leftover_events = [
        ev_id for ev_id in full_conflicted_set if ev_id not in set_power_events
    ]

    logger.debug("sorting %d remaining events", len(leftover_events))

    pl = resolved_state.get((EventTypes.PowerLevels, ""), None)
    leftover_events = yield _mainline_sort(
        clock, room_id, leftover_events, pl, event_map, state_res_store
    )

    logger.debug("resolving remaining events")

    resolved_state = yield _iterative_auth_checks(
        clock,
        room_id,
        room_version,
        leftover_events,
        resolved_state,
        event_map,
        state_res_store,
    )

    logger.debug("resolved")

    # We make sure that unconflicted state always still applies.
    resolved_state.update(unconflicted_state)

    logger.debug("done")

    return resolved_state


@defer.inlineCallbacks
def _get_power_level_for_sender(room_id, event_id, event_map, state_res_store):
    """Return the power level of the sender of the given event according to
    their auth events.

    Args:
        room_id (str)
        event_id (str)
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)

    Returns:
        Deferred[int]
    """
    event = yield _get_event(room_id, event_id, event_map, state_res_store)

    pl = None
    for aid in event.auth_event_ids():
        aev = yield _get_event(
            room_id, aid, event_map, state_res_store, allow_none=True
        )
        if aev and (aev.type, aev.state_key) == (EventTypes.PowerLevels, ""):
            pl = aev
            break

    if pl is None:
        # Couldn't find power level. Check if they're the creator of the room
        for aid in event.auth_event_ids():
            aev = yield _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )
            if aev and (aev.type, aev.state_key) == (EventTypes.Create, ""):
                if aev.content.get("creator") == event.sender:
                    return 100
                break
        return 0

    level = pl.content.get("users", {}).get(event.sender)
    if level is None:
        level = pl.content.get("users_default", 0)

    if level is None:
        return 0
    else:
        return int(level)


@defer.inlineCallbacks
def _get_auth_chain_difference(state_sets, event_map, state_res_store):
    """Compare the auth chains of each state set and return the set of events
    that only appear in some but not all of the auth chains.

    Args:
        state_sets (list)
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)

    Returns:
        Deferred[set[str]]: Set of event IDs
    """

    difference = yield state_res_store.get_auth_chain_difference(
        [set(state_set.values()) for state_set in state_sets]
    )

    return difference


def _seperate(state_sets):
    """Return the unconflicted and conflicted state. This is different than in
    the original algorithm, as this defines a key to be conflicted if one of
    the state sets doesn't have that key.

    Args:
        state_sets (list)

    Returns:
        tuple[dict, dict]: A tuple of unconflicted and conflicted state. The
        conflicted state dict is a map from type/state_key to set of event IDs
    """
    unconflicted_state = {}
    conflicted_state = {}

    for key in set(itertools.chain.from_iterable(state_sets)):
        event_ids = {state_set.get(key) for state_set in state_sets}
        if len(event_ids) == 1:
            unconflicted_state[key] = event_ids.pop()
        else:
            event_ids.discard(None)
            conflicted_state[key] = event_ids

    return unconflicted_state, conflicted_state


def _is_power_event(event):
    """Return whether or not the event is a "power event", as defined by the
    v2 state resolution algorithm

    Args:
        event (FrozenEvent)

    Returns:
        boolean
    """
    if (event.type, event.state_key) in (
        (EventTypes.PowerLevels, ""),
        (EventTypes.JoinRules, ""),
        (EventTypes.Create, ""),
    ):
        return True

    if event.type == EventTypes.Member:
        if event.membership in ("leave", "ban"):
            return event.sender != event.state_key

    return False


@defer.inlineCallbacks
def _add_event_and_auth_chain_to_graph(
    graph, room_id, event_id, event_map, state_res_store, auth_diff
):
    """Helper function for _reverse_topological_power_sort that add the event
    and its auth chain (that is in the auth diff) to the graph

    Args:
        graph (dict[str, set[str]]): A map from event ID to the events auth
            event IDs
        room_id (str): the room we are working in
        event_id (str): Event to add to the graph
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)
        auth_diff (set[str]): Set of event IDs that are in the auth difference.
    """

    state = [event_id]
    while state:
        eid = state.pop()
        graph.setdefault(eid, set())

        event = yield _get_event(room_id, eid, event_map, state_res_store)
        for aid in event.auth_event_ids():
            if aid in auth_diff:
                if aid not in graph:
                    state.append(aid)

                graph.setdefault(eid, set()).add(aid)


@defer.inlineCallbacks
def _reverse_topological_power_sort(
    clock, room_id, event_ids, event_map, state_res_store, auth_diff
):
    """Returns a list of the event_ids sorted by reverse topological ordering,
    and then by power level and origin_server_ts

    Args:
        clock (Clock)
        room_id (str): the room we are working in
        event_ids (list[str]): The events to sort
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)
        auth_diff (set[str]): Set of event IDs that are in the auth difference.

    Returns:
        Deferred[list[str]]: The sorted list
    """

    graph = {}
    for idx, event_id in enumerate(event_ids, start=1):
        yield _add_event_and_auth_chain_to_graph(
            graph, room_id, event_id, event_map, state_res_store, auth_diff
        )

        # We yield occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _YIELD_AFTER_ITERATIONS == 0:
            yield clock.sleep(0)

    event_to_pl = {}
    for idx, event_id in enumerate(graph, start=1):
        pl = yield _get_power_level_for_sender(
            room_id, event_id, event_map, state_res_store
        )
        event_to_pl[event_id] = pl

        # We yield occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _YIELD_AFTER_ITERATIONS == 0:
            yield clock.sleep(0)

    def _get_power_order(event_id):
        ev = event_map[event_id]
        pl = event_to_pl[event_id]

        return -pl, ev.origin_server_ts, event_id

    # Note: graph is modified during the sort
    it = lexicographical_topological_sort(graph, key=_get_power_order)
    sorted_events = list(it)

    return sorted_events


@defer.inlineCallbacks
def _iterative_auth_checks(
    clock, room_id, room_version, event_ids, base_state, event_map, state_res_store
):
    """Sequentially apply auth checks to each event in given list, updating the
    state as it goes along.

    Args:
        clock (Clock)
        room_id (str)
        room_version (str)
        event_ids (list[str]): Ordered list of events to apply auth checks to
        base_state (StateMap[str]): The set of state to start with
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)

    Returns:
        Deferred[StateMap[str]]: Returns the final updated state
    """
    resolved_state = base_state.copy()
    room_version_obj = KNOWN_ROOM_VERSIONS[room_version]

    for idx, event_id in enumerate(event_ids, start=1):
        event = event_map[event_id]

        auth_events = {}
        for aid in event.auth_event_ids():
            ev = yield _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )

            if not ev:
                logger.warning(
                    "auth_event id %s for event %s is missing", aid, event_id
                )
            else:
                if ev.rejected_reason is None:
                    auth_events[(ev.type, ev.state_key)] = ev

        for key in event_auth.auth_types_for_event(event):
            if key in resolved_state:
                ev_id = resolved_state[key]
                ev = yield _get_event(room_id, ev_id, event_map, state_res_store)

                if ev.rejected_reason is None:
                    auth_events[key] = event_map[ev_id]

        try:
            event_auth.check(
                room_version_obj,
                event,
                auth_events,
                do_sig_check=False,
                do_size_check=False,
            )

            resolved_state[(event.type, event.state_key)] = event_id
        except AuthError:
            pass

        # We yield occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _YIELD_AFTER_ITERATIONS == 0:
            yield clock.sleep(0)

    return resolved_state


@defer.inlineCallbacks
def _mainline_sort(
    clock, room_id, event_ids, resolved_power_event_id, event_map, state_res_store
):
    """Returns a sorted list of event_ids sorted by mainline ordering based on
    the given event resolved_power_event_id

    Args:
        clock (Clock)
        room_id (str): room we're working in
        event_ids (list[str]): Events to sort
        resolved_power_event_id (str): The final resolved power level event ID
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)

    Returns:
        Deferred[list[str]]: The sorted list
    """
    if not event_ids:
        # It's possible for there to be no event IDs here to sort, so we can
        # skip calculating the mainline in that case.
        return []

    mainline = []
    pl = resolved_power_event_id
    idx = 0
    while pl:
        mainline.append(pl)
        pl_ev = yield _get_event(room_id, pl, event_map, state_res_store)
        auth_events = pl_ev.auth_event_ids()
        pl = None
        for aid in auth_events:
            ev = yield _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )
            if ev and (ev.type, ev.state_key) == (EventTypes.PowerLevels, ""):
                pl = aid
                break

        # We yield occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx != 0 and idx % _YIELD_AFTER_ITERATIONS == 0:
            yield clock.sleep(0)

        idx += 1

    mainline_map = {ev_id: i + 1 for i, ev_id in enumerate(reversed(mainline))}

    event_ids = list(event_ids)

    order_map = {}
    for idx, ev_id in enumerate(event_ids, start=1):
        depth = yield _get_mainline_depth_for_event(
            event_map[ev_id], mainline_map, event_map, state_res_store
        )
        order_map[ev_id] = (depth, event_map[ev_id].origin_server_ts, ev_id)

        # We yield occasionally when we're working with large data sets to
        # ensure that we don't block the reactor loop for too long.
        if idx % _YIELD_AFTER_ITERATIONS == 0:
            yield clock.sleep(0)

    event_ids.sort(key=lambda ev_id: order_map[ev_id])

    return event_ids


@defer.inlineCallbacks
def _get_mainline_depth_for_event(event, mainline_map, event_map, state_res_store):
    """Get the mainline depths for the given event based on the mainline map

    Args:
        event (FrozenEvent)
        mainline_map (dict[str, int]): Map from event_id to mainline depth for
            events in the mainline.
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)

    Returns:
        Deferred[int]
    """

    room_id = event.room_id

    # We do an iterative search, replacing `event with the power level in its
    # auth events (if any)
    while event:
        depth = mainline_map.get(event.event_id)
        if depth is not None:
            return depth

        auth_events = event.auth_event_ids()
        event = None

        for aid in auth_events:
            aev = yield _get_event(
                room_id, aid, event_map, state_res_store, allow_none=True
            )
            if aev and (aev.type, aev.state_key) == (EventTypes.PowerLevels, ""):
                event = aev
                break

    # Didn't find a power level auth event, so we just return 0
    return 0


@defer.inlineCallbacks
def _get_event(room_id, event_id, event_map, state_res_store, allow_none=False):
    """Helper function to look up event in event_map, falling back to looking
    it up in the store

    Args:
        room_id (str)
        event_id (str)
        event_map (dict[str,FrozenEvent])
        state_res_store (StateResolutionStore)
        allow_none (bool): if the event is not found, return None rather than raising
            an exception

    Returns:
        Deferred[Optional[FrozenEvent]]
    """
    if event_id not in event_map:
        events = yield state_res_store.get_events([event_id], allow_rejected=True)
        event_map.update(events)
    event = event_map.get(event_id)

    if event is None:
        if allow_none:
            return None
        raise Exception("Unknown event %s" % (event_id,))

    if event.room_id != room_id:
        raise Exception(
            "In state res for room %s, event %s is in %s"
            % (room_id, event_id, event.room_id)
        )
    return event


def lexicographical_topological_sort(graph, key):
    """Performs a lexicographic reverse topological sort on the graph.

    This returns a reverse topological sort (i.e. if node A references B then B
    appears before A in the sort), with ties broken lexicographically based on
    return value of the `key` function.

    NOTE: `graph` is modified during the sort.

    Args:
        graph (dict[str, set[str]]): A representation of the graph where each
            node is a key in the dict and its value are the nodes edges.
        key (func): A function that takes a node and returns a value that is
            comparable and used to order nodes

    Yields:
        str: The next node in the topological sort
    """

    # Note, this is basically Kahn's algorithm except we look at nodes with no
    # outgoing edges, c.f.
    # https://en.wikipedia.org/wiki/Topological_sorting#Kahn's_algorithm
    outdegree_map = graph
    reverse_graph = {}

    # Lists of nodes with zero out degree. Is actually a tuple of
    # `(key(node), node)` so that sorting does the right thing
    zero_outdegree = []

    for node, edges in graph.items():
        if len(edges) == 0:
            zero_outdegree.append((key(node), node))

        reverse_graph.setdefault(node, set())
        for edge in edges:
            reverse_graph.setdefault(edge, set()).add(node)

    # heapq is a built in implementation of a sorted queue.
    heapq.heapify(zero_outdegree)

    while zero_outdegree:
        _, node = heapq.heappop(zero_outdegree)

        for parent in reverse_graph[node]:
            out = outdegree_map[parent]
            out.discard(node)
            if len(out) == 0:
                heapq.heappush(zero_outdegree, (key(parent), parent))

        yield node
