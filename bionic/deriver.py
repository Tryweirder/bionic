"""
Contains the core logic for resolving Entities by executing Tasks.
"""

from collections import defaultdict

from .datatypes import ProvenanceDigest, Query, Result, ResultGroup
from .cache import Provenance
from .descriptors import DescriptorNode
from .exception import UndefinedEntityError, CodeVersioningError
from .optdep import import_optional_dependency
from .util import oneline

import logging

# TODO At some point it might be good to have the option of Bionic handling its
# own logging.  Probably it would manage its own logger instances and inject
# them into tasks, while providing the option of either handling the output
# itself or routing it back to the global logging system.
logger = logging.getLogger(__name__)


class FlowDeriver:
    """
    Derives the values of descriptors in a flow.

    This is the class that constructs the descriptor graph and computes the value
    or values of each descriptor.
    """

    # --- Public API.

    def __init__(self, flow_state, flow_instance_uuid):
        self._flow_instance_uuid = flow_instance_uuid
        self._flow_state = flow_state
        self._plan = FlowPlan(flow_state)
        self._bootstrap = None

    def get_ready(self):
        """
        Make sure this Deriver is ready to derive().  Calling this is not
        necessary but allows errors to surface earlier.
        """
        self._initialize_bootstrap()

    def derive(self, dnode):
        """
        Given a descriptor node, computes and returns a ResultGroup containing
        all values for that descriptor.
        """
        self.get_ready()
        return self._create_execution().derive_result_group_for_dnode(dnode)

    def export_dag(self, include_core=False):
        """
        Constructs a NetworkX graph corresponding to the DAG of tasks.  There
        is one node per task key -- i.e., for each artifact that can be created
        (uniquely defined by an entity name and a case key); and one edge from
        each task key to each key that depends on it.  Each node is represented
        by a TaskKey, and also has the following attributes:

            name: a short, unique, human-readable identifier
            entity_name: the name of the entity for this task key
            case_key: the case key for this task key
            task_ix: the task key's index in the ordered series of case keys
                     for its entity
        """
        nx = import_optional_dependency("networkx", purpose="constructing the flow DAG")

        def should_include_entity_name(name):
            return include_core or not self.entity_is_internal(entity_name)

        self.get_ready()

        graph = nx.DiGraph()

        for dnode in self._plan.all_dnodes():
            tasks = self._plan.tasks_for_dnode(dnode)
            entity_name = dnode.to_descriptor()
            if not should_include_entity_name(entity_name):
                continue

            provider = self._flow_state.get_provider(entity_name)
            doc = provider.doc_for_name(entity_name)

            if len(tasks) == 1:
                name_template = "{entity_name}"
            else:
                name_template = "{entity_name}[{task_ix}]"

            for task_ix, task in enumerate(
                sorted(tasks, key=lambda task: task.keys[0].case_key)
            ):
                task_key = task.key_for_entity_name(entity_name)
                task_plan = self._plan.task_plan_for_key(task_key)

                node_name = name_template.format(
                    entity_name=entity_name, task_ix=task_ix
                )

                graph.add_node(
                    task_key,
                    name=node_name,
                    entity_name=entity_name,
                    case_key=task_key.case_key,
                    task_ix=task_ix,
                    doc=doc,
                )

                for dep_plan in task_plan.dep_plans:
                    for dep_task_key in dep_plan.task.keys:
                        graph.add_edge(dep_task_key, task_key)

        return graph

    def entity_is_internal(self, entity_name):
        return entity_name.startswith("core__")

    # --- Private helpers.

    def _create_execution(self):
        return FlowExecution(
            plan=self._plan,
            flow_instance_uuid=self._flow_instance_uuid,
            bootstrap=self._bootstrap,
        )

    def _initialize_bootstrap(self):
        if self._bootstrap is not None:
            return

        self._plan.set_up_all_descriptor_state()

        self._bootstrap = Bootstrap(
            persistent_cache=self._bootstrap_singleton_entity(
                "core__persistent_cache"
            ),
            versioning_policy=self._bootstrap_singleton_entity(
                "core__versioning_policy"
            ),
        )

    def _bootstrap_singleton_entity(self, entity_name):
        assert self._bootstrap is None

        dnode = DescriptorNode.from_descriptor(entity_name)
        result_group = self._create_execution().derive_result_group_for_dnode(dnode)
        if len(result_group) == 0:
            raise ValueError(
                oneline(
                    f"""
                No values were defined for internal bootstrap entity
                {entity_name!r}"""
                )
            )
        if len(result_group) > 1:
            values = [result.value for result in result_group]
            raise ValueError(
                oneline(
                    f"""
                Bootstrap entity {entity_name!r} must have exactly one
                value; got {len(values)} ({values!r})"""
                )
            )
        return result_group[0].value


class FlowPlan(object):
    """
    Represents a plan for how to compute descriptor values in a flow. This includes
    a mapping from descriptor nodes to data about their dependencies, provenance, cached
    values, etc. This data all stays valid throughout the lifetime of a flow.
    """

    # --- Public API.

    def __init__(self, flow_state):
        self._flow_state = flow_state

        # This state is needed to do any resolution at all.  Once it's
        # initialized, we can use it to bootstrap the requirements for "full"
        # resolution below.
        self._key_spaces_by_dnode = None
        self._task_lists_by_dnode = None
        self._task_plans_by_key = None
        self._docs_by_entity_name = {}
        self._descriptor_state_is_set_up = False

    def set_up_all_descriptor_state(self):
        if self._descriptor_state_is_set_up:
            return

        # Generate the static key spaces and tasks for each descriptor.
        self._key_spaces_by_dnode = {}
        self._task_lists_by_dnode = {}
        for name in self._flow_state.providers_by_name.keys():
            dnode = DescriptorNode.from_descriptor(name)
            self._populate_dnode_info(dnode)

        # Create a lookup table for all tasks.
        self._tasks_by_key = {}
        for tasks in self._task_lists_by_dnode.values():
            for task in tasks:
                for task_key in task.keys:
                    self._tasks_by_key[task_key] = task

        # Create a plan object for each task.
        self._task_plans_by_key = {}
        for task_key in self._tasks_by_key.keys():
            self._get_or_create_task_plan_for_key(task_key)

        self._descriptor_state_is_set_up = True

    def tasks_for_dnode(self, dnode):
        return self._task_lists_by_dnode[dnode]

    def task_plan_for_key(self, task_key):
        return self._task_plans_by_key[task_key]

    def key_space_for_dnode(self, dnode):
        return self._key_spaces_by_dnode[dnode]

    def all_dnodes(self):
        return self._task_lists_by_dnode.keys()

    # --- Private helpers.

    def _get_or_create_task_plan_for_key(self, task_key):
        if task_key in self._task_plans_by_key:
            return self._task_plans_by_key[task_key]

        task = self._tasks_by_key[task_key]
        dep_plans = [
            self._get_or_create_task_plan_for_key(dep_key) for dep_key in task.dep_keys
        ]
        # All keys in this task should point to the same provider, so the set below
        # should have exactly one element.
        (provider,) = set(
            self._flow_state.get_provider(task_key.dnode.to_entity_name())
            for task_key in task.keys
        )
        # And all the task keys should have the same case key.
        (case_key,) = set(task_key.case_key for task_key in task.keys)

        task_plan = TaskPlan(
            task=task, dep_plans=dep_plans, provider=provider, case_key=case_key,
        )

        for task_key in task.keys:
            self._task_plans_by_key[task_key] = task_plan
        return task_plan

    def _populate_dnode_info(self, dnode):
        if dnode in self._task_lists_by_dnode:
            return

        entity_name = dnode.to_entity_name()
        provider = self._flow_state.get_provider(entity_name)

        dep_dnodes = provider.get_dependency_dnodes()
        for dep_dnode in dep_dnodes:
            self._populate_dnode_info(dep_dnode)

        dep_key_spaces_by_dnode = {
            dep_dnode: self._key_spaces_by_dnode[dep_dnode] for dep_dnode in dep_dnodes
        }

        dep_task_key_lists_by_dnode = {
            dep_dnode: [
                task.key_for_entity_name(dep_dnode.to_entity_name())
                for task in self._task_lists_by_dnode[dep_dnode]
            ]
            for dep_dnode in dep_dnodes
        }

        self._key_spaces_by_dnode[dnode] = provider.get_key_space(
            dep_key_spaces_by_dnode
        )
        self._task_lists_by_dnode[dnode] = provider.get_tasks(
            dep_key_spaces_by_dnode, dep_task_key_lists_by_dnode
        )

        self._docs_by_entity_name[entity_name] = provider.doc_for_name(entity_name)


class Bootstrap:
    def __init__(self, persistent_cache, versioning_policy):
        self.persistent_cache = persistent_cache
        self.versioning_policy = versioning_policy


class FlowExecution:
    """
    FIXME
    """

    def __init__(self, plan, flow_instance_uuid, bootstrap):
        self._plan = plan
        self._flow_instance_uuid = flow_instance_uuid
        self._bootstrap = bootstrap

        log_level = logging.INFO if self._bootstrap is not None else logging.DEBUG
        self._task_key_logger = TaskKeyLogger(log_level)
        self._blockage_tracker = TaskBlockageTracker()

    def derive_result_group_for_dnode(self, dnode):
        entity_name = dnode.to_entity_name()
        try:
            tasks = self._plan.tasks_for_dnode(dnode)
        except KeyError:
            raise UndefinedEntityError.for_name(entity_name)
        requested_task_plans = [
            self._plan.task_plan_for_key(task.keys[0]) for task in tasks
        ]

        ready_task_excns = [
            TaskExecution(task_plan)
            for task_plan in requested_task_plans
        ]

        while ready_task_excns:
            task_excn = ready_task_excns.pop()

            # If this task is already complete, we don't need to do any work.
            # But if this is the first time we've seen this task, we should
            # should log a message.
            # FIXME Since we have logging in _get_results_for_..., do we need to log
            # here too?
            if task_excn.plan.is_complete:
                for task_key in task_excn.plan.task.keys:
                    self._task_key_logger.log_accessed_from_memory(task_key)
                continue

            # If blocked, let's mark it and try to derive its dependencies.
            incomplete_dep_plans = task_excn.plan.incomplete_dep_plans()
            if incomplete_dep_plans:
                self._blockage_tracker.add_blockage(
                    blocked_plan=task_excn.plan, blocking_plans=incomplete_dep_plans,
                )
                incomplete_dep_excns = [
                    TaskExecution(dep_plan)
                    for dep_plan in incomplete_dep_plans
                ]
                ready_task_excns.extend(incomplete_dep_excns)
                continue

            # If the task isn't complete or blocked, we can complete the task.
            self._complete_task_plan(task_excn.plan)

            # See if we can unblock any other plans now that we've completed this one.
            unblocked_plans = self._blockage_tracker.get_unblocked_by(task_excn.plan)
            unblocked_excns = [
                TaskExecution(task_plan)
                for task_plan in unblocked_plans
            ]
            ready_task_excns.extend(unblocked_excns)

        blocked_plans = self._blockage_tracker.get_all_blocked_plans()
        assert not blocked_plans, blocked_plans

        for task_plan in requested_task_plans:
            assert task_plan.is_complete, task_plan

        return ResultGroup(
            results=[
                self._get_results_for_complete_task_plan(task_plan)[
                    entity_name
                ]
                for task_plan in requested_task_plans
            ],
            key_space=self._plan.key_space_for_dnode(dnode),
        )

    def add_blockage(self, blocked_excn, blocking_excn):
        blocked_excn.blocking_excns.add(blocking_excn)
        blocking_excn.blocked_excns.add(blocked_excn)

    def remove_blockage(self, blocked_excn, blocking_excn):
        blocked_excn.blocking_excns.remove(blocking_excn)
        blocking_excn.blocked_excns.remove(blocked_excn)

    # -- Private per-task operations.

    def _check_accessors_for_version_problems(self, task_plan):
        accessors_needing_saving = []
        for accessor in task_plan.cache_accessors:
            old_prov = accessor.load_provenance()

            if old_prov is None:
                continue

            new_prov = accessor.query.provenance

            if old_prov.exactly_matches(new_prov):
                continue
            accessors_needing_saving.append(accessor)

            if old_prov.code_version_minor == new_prov.code_version_minor:
                if old_prov.bytecode_hash != new_prov.bytecode_hash:
                    raise CodeVersioningError(
                        oneline(
                            f"""
                        Found a cached artifact with the same
                        descriptor ({accessor.query.dnode.to_descriptor()!r}) and
                        version (major={old_prov.code_version_major!r},
                        minor={old_prov.code_version_minor!r}),
                        But created by different code
                        (old hash {old_prov.bytecode_hash!r},
                        new hash {new_prov.bytecode_hash!r}).
                        Did you change your code but not update the
                        version number?
                        Change @version(major=) to indicate that your
                        function's behavior has changed, or @version(minor=)
                        to indicate that it has *not* changed."""
                        )
                    )

        for accessor in accessors_needing_saving:
            accessor.update_provenance()

    def _complete_task_plan(self, task_plan):
        assert not task_plan.is_blocked
        assert not task_plan.is_complete

        # First, set up provenance.
        if self._bootstrap is None:
            # If we're still in the bootstrap resolution phase, we don't have
            # any versioning policy, so we don't attempt anything fancy.
            treat_bytecode_as_functional = False
        else:
            treat_bytecode_as_functional = (
                self._bootstrap.versioning_policy.treat_bytecode_as_functional
            )

        dep_provenance_digests_by_task_key = {}
        for dep_key, dep_plan in zip(task_plan.task.dep_keys, task_plan.dep_plans):
            # Use value hash of persistable values.
            if dep_plan.provider.attrs.should_persist():
                value_hash = dep_plan.result_value_hashes_by_name[
                    dep_key.dnode.to_entity_name()
                ]
                dep_provenance_digests_by_task_key[
                    dep_key
                ] = ProvenanceDigest.from_value_hash(value_hash)
            # Otherwise, use the provenance.
            else:
                dep_provenance_digests_by_task_key[
                    dep_key
                ] = ProvenanceDigest.from_provenance(dep_plan.provenance)

        task_plan.provenance = Provenance.from_computation(
            code_fingerprint=task_plan.provider.get_code_fingerprint(
                task_plan.case_key
            ),
            case_key=task_plan.case_key,
            dep_provenance_digests_by_task_key=dep_provenance_digests_by_task_key,
            treat_bytecode_as_functional=treat_bytecode_as_functional,
            can_functionally_change_per_run=task_plan.provider.attrs.changes_per_run,
            flow_instance_uuid=self._flow_instance_uuid,
        )

        # Then set up queries.
        task_plan.queries = [
            Query(
                task_key=task_key,
                protocol=task_plan.provider.protocol_for_name(
                    task_key.dnode.to_entity_name()
                ),
                provenance=task_plan.provenance,
            )
            for task_key in task_plan.task.keys
        ]

        # Lastly, set up cache accessors.
        if task_plan.provider.attrs.should_persist():
            if self._bootstrap is None:
                name = task_plan.task.keys[0].entity_name
                raise AssertionError(
                    oneline(
                        f"""
                    Attempting to load cached state for entity {name!r},
                    but the cache is not available yet because core bootstrap
                    entities depend on this one;
                    you should decorate entity {name!r} with `@persist(False)`
                    or `@immediate` to indicate that it can't be cached."""
                    )
                )

            task_plan.cache_accessors = [
                self._bootstrap.persistent_cache.get_accessor(query)
                for query in task_plan.queries
            ]

            if self._bootstrap.versioning_policy.check_for_bytecode_errors:
                self._check_accessors_for_version_problems(task_plan)

        # See if we can load it from the cache.
        if task_plan.provider.attrs.should_persist() and all(
            axr.can_load() for axr in task_plan.cache_accessors
        ):
            # We only load the hashed result while completing task plan
            # and lazily load the entire result when needed later.
            value_hashes_by_name = {}
            for accessor in task_plan.cache_accessors:
                value_hash = accessor.load_result_value_hash()
                value_hashes_by_name[accessor.query.dnode.to_entity_name()] = value_hash

            task_plan.result_value_hashes_by_name = value_hashes_by_name
        # If we cannot load it from cache, we compute the task plan.
        else:
            self._compute_task_plan(task_plan)

        task_plan.is_complete = True

    def _compute_task_plan(self, task_plan):
        task = task_plan.task
        dep_keys = task.dep_keys
        dep_results = [
            self._get_results_for_complete_task_plan(
                self._plan.task_plan_for_key(dep_key)
            )[dep_key.dnode.to_entity_name()]
            for dep_key in dep_keys
        ]

        provider = task_plan.provider

        if not task.is_simple_lookup:
            for task_key in task.keys:
                self._task_key_logger.log_computing(task_key)

        dep_values = [dep_result.value for dep_result in dep_results]

        values = task_plan.task.compute(dep_values)
        assert len(values) == len(provider.attrs.names)

        for query in task_plan.queries:
            if task.is_simple_lookup:
                self._task_key_logger.log_accessed_from_definition(query.task_key)
            else:
                self._task_key_logger.log_computed(query.task_key)

        results_by_name = {}
        result_value_hashes_by_name = {}
        for ix, (query, value) in enumerate(zip(task_plan.queries, values)):
            query.protocol.validate(value)

            result = Result(query=query, value=value,)

            if provider.attrs.should_persist():
                accessor = task_plan.cache_accessors[ix]
                accessor.save_result(result)

                value_hash = accessor.load_result_value_hash()
                result_value_hashes_by_name[query.dnode.to_entity_name()] = value_hash

            results_by_name[query.dnode.to_entity_name()] = result

        # Memoize results at this point only if results should not persist.
        # Otherwise, load it lazily later so that if the serialized/deserialized
        # value is not exactly the same as the original, we still
        # always return the same value.
        if provider.attrs.should_memoize() and not provider.attrs.should_persist():
            task_plan._results_by_name = results_by_name

        # But we cache the hashed values eagerly since they are cheap to load.
        if provider.attrs.should_persist():
            task_plan.result_value_hashes_by_name = result_value_hashes_by_name

    def _get_results_for_complete_task_plan(self, task_plan):
        assert task_plan.is_complete

        if task_plan._results_by_name:
            for task_key in task_plan.task.keys:
                self._task_key_logger.log_accessed_from_memory(task_key)
            return task_plan._results_by_name

        results_by_name = dict()
        for accessor in task_plan.cache_accessors:
            result = accessor.load_result()
            self._task_key_logger.log_loaded_from_disk(result.query.task_key)

            # Make sure the result is saved in all caches under this exact
            # query.
            accessor.save_result(result)

            results_by_name[result.query.dnode.to_entity_name()] = result

        if task_plan.provider.attrs.should_memoize():
            task_plan._results_by_name = results_by_name

        return results_by_name


class TaskKeyLogger:
    """
    Logs how we derived each task key. The purpose of this class is to make sure that
    each task key used in an execution (i.e., a call to `Flow.get()`) is logged exactly
    once. (One exception: a task key can be logged twice to indicate the start and end
    of a computation.)
    """

    def __init__(self, level):
        self._level = level
        self._already_logged_task_keys = set()

    def _log(self, template, task_key, is_resolved=True):
        if task_key in self._already_logged_task_keys:
            return
        logger.log(self._level, template, task_key)
        if is_resolved:
            self._already_logged_task_keys.add(task_key)

    def log_accessed_from_memory(self, task_key):
        self._log("Accessed   %s from in-memory cache", task_key)

    def log_accessed_from_definition(self, task_key):
        self._log("Accessed   %s from definition", task_key)

    def log_loaded_from_disk(self, task_key):
        self._log("Loaded     %s from disk cache", task_key)

    def log_computing(self, task_key):
        self._log("Computing  %s ...", task_key, is_resolved=False)

    def log_computed(self, task_key):
        self._log("Computed   %s", task_key)


class TaskPlan(object):
    """
    Represents a plan for computing a task; mostly a holder for extra helper data needed
    to compute the task's value.
    """

    def __init__(self, task, dep_plans, case_key, provider):
        self.task = task
        self.dep_plans = dep_plans
        self.case_key = case_key
        self.provider = provider

        # These are set by FlowExecution._complete_task_plan(), just
        # before the task plan becomes eligible for cache lookup / computation.
        #
        # They will be present if and only if is_complete is True.
        self.provenance = None
        self.queries = None
        self.cache_accessors = None

        # This can be set by
        # FlowExecution._complete_task_plan() or
        # FlowExecution._compute_task_plan().
        #
        # This will be present if and only if both is_complete and
        # provider.attrs.should_persist() are True.
        self.result_value_hashes_by_name = None

        # This can be set by
        # FlowExecution._get_results_for_complete_task_plan() or
        # FlowExecution._compute_task_plan().
        #
        # This should never be accessed directly; instead, use
        # FlowExecution._get_results_for_complete_task_plan().
        self._results_by_name = None

        self.is_complete = False

    def incomplete_dep_plans(self):
        return [dep_plan for dep_plan in self.dep_plans if not dep_plan.is_complete]

    @property
    def is_blocked(self):
        return len(self.incomplete_dep_plans()) > 0

    def __repr__(self):
        return f"TaskPlan({self.task!r})"


class TaskExecution:
    def __init__(self, task_plan):
        self.plan = task_plan

        self.blocked_excns = set()
        self.blocking_excns = set()


# FIXME Move this into TaskExecution class?
class TaskBlockage:
    """
    Represents a blocking relationship between a task plan and a collection of
    not-yet-completed task keys it depends on.
    """

    def __init__(self, blocked_plan, blocking_tks):
        self.blocked_plan = blocked_plan
        self._blocking_tks = set(blocking_tks)

    def mark_task_key_complete(self, blocking_tk):
        self._blocking_tks.discard(blocking_tk)

    def is_resolved(self):
        return not self._blocking_tks


class TaskBlockageTracker:
    """
    A helper class that keeps track of which task plans are blocked by others.

    A task plan X is "blocked" by another task plan Y if X depends on Y and Y is
    not complete.
    """

    def __init__(self):
        self._blockage_lists_by_blocking_tk = defaultdict(list)

    def add_blockage(self, blocked_plan, blocking_plans):
        """Records the fact that one task plan is blocked by certain others."""

        blocking_tks = [
            blocking_tk
            for blocking_plan in blocking_plans
            for blocking_tk in blocking_plan.task.keys
        ]
        blockage = TaskBlockage(blocked_plan, blocking_tks)
        for blocking_tk in blocking_tks:
            self._blockage_lists_by_blocking_tk[blocking_tk].append(blockage)

    def get_unblocked_by(self, completed_plan):
        """
        Records the fact that a task plan is complete, and yields all task plans
        that are newly unblocked.
        """

        for completed_tk in completed_plan.task.keys:
            affected_blockages = self._blockage_lists_by_blocking_tk[completed_tk]
            for blockage in affected_blockages:
                blockage.mark_task_key_complete(completed_tk)
                if blockage.is_resolved():
                    yield blockage.blocked_plan

    def get_all_blocked_plans(self):
        return {
            blockage.blocked_plan
            for blockages in self._blockage_lists_by_blocking_tk.values()
            for blockage in blockages
            if not blockage.is_resolved()
        }
