"""Budgeted local and complete bounded queries. SAT is not intent evidence."""
import time
from .expressions import Unsupported, parse, evaluate
from .schema import digest
from .transition_system import TransitionSystem


def unknown(assumption_id, alternative_id, level, reason, status='unknown'):
    return dict(assumption_id=assumption_id, alternative_id=alternative_id, level=level,
                status=status, reason=str(reason), witness=None, replay_status='not_run',
                claim_kind='local_next_state_difference' if level == 'local' else 'semantic_impact_within_K')


def _model_values(solution, values):
    return {name: solution.eval(value, model_completion=True).as_long() for name, value in values.items()}


def _constraints(system, state, inputs, constraints):
    conditions = []
    for item in constraints:
        if not item.get('evidence_ids'):
            raise Unsupported('environment constraint without source evidence')
        env = system.environment(state, inputs)
        conditions.append(evaluate(parse(item['expression']), env, system.widths) != 0)
    return conditions


def _solve(solver, condition, budget_ms):
    import z3
    solver.set(timeout=budget_ms)
    solver.push()
    solver.add(condition)
    started = time.monotonic()
    result = solver.check()
    elapsed = (time.monotonic()-started)*1000
    status = str(result)
    reason = solver.reason_unknown() if result == z3.unknown else None
    solution = solver.model() if result == z3.sat else None
    solver.pop()
    return status, reason, solution, elapsed


def local_difference(current, alternative, assumption_id, alternative_id, *, budget_ms=2000, environment=()):
    if budget_ms < 1:
        raise ValueError('solver budget must be positive')
    try:
        import z3
        a, b = TransitionSystem(current), TransitionSystem(alternative)
        state, inputs = a.variables('local_state', a.states), a.variables('local_input', a.inputs)
        na, _ = a.step(state, inputs)
        nb, _ = b.step(state, inputs)
        solver = z3.Solver()
        solver.add(*_constraints(a, state, inputs, environment))
        satisfiable, reason, _, _ = _solve(solver, z3.BoolVal(True), budget_ms)
        if satisfiable != 'sat':
            return unknown(assumption_id, alternative_id, 'local', reason or 'empty environment',
                           'invalid_environment' if satisfiable == 'unsat' else 'unknown')
        condition = z3.Or(*(na[n] != nb[n] for n in a.states))
        status, reason, solution, elapsed = _solve(solver, condition, budget_ms)
        result = unknown(assumption_id, alternative_id, 'local', reason or '', status)
        result.update(budget_ms=budget_ms, elapsed_ms=elapsed, query_hash=digest(solver.sexpr()+condition.sexpr()),
                      tool_version=z3.get_version_string(), visibility='G1',
                      scope=dict(semantics='two_state', observation_domain=a.observation_domain,
                          reset_type=a.domain.reset_type, inter_edge_reset_pulses=False,
                          state_widths={n:a.widths[n] for n in a.states}, initial_state='abstract'))
        if solution is not None:
            result['witness'] = dict(state=_model_values(solution, state), inputs=_model_values(solution, inputs),
                                     next_A=_model_values(solution, na), next_B=_model_values(solution, nb))
        return result
    except (Unsupported, ImportError, KeyError) as exc:
        return unknown(assumption_id, alternative_id, 'local', exc)


def bounded_impact(current, alternative, assumption_id, alternative_id, *, horizon=16, budget_ms=2000, environment=(), output_protocol=None):
    if horizon < 1 or budget_ms < 1:
        raise ValueError('horizon and solver budget must be positive')
    try:
        import z3
        a, b = TransitionSystem(current), TransitionSystem(alternative)
        sa, sb, prefix = a.initial_pair(b)
        initial_a, initial_b = sa, sb
        solver = z3.Solver()
        if prefix is not None:
            # Reset-prefix environment legality must also be checked.
            before = a.variables('initial', a.states)
            solver.add(*_constraints(a, before, prefix, environment))
        frames, differences = [], {}
        def observe(name, va, vb):
            differences.setdefault(name, []).append(va != vb)
        for name in a.states:
            observe('state:' + name, sa[name], sb[name])
        for step in range(horizon):
            inputs = a.variables(f'input_{step}', a.inputs)
            solver.add(*_constraints(a, sa, inputs, environment))
            solver.add(*_constraints(b, sb, inputs, environment))
            na, oa = a.step(sa, inputs)
            nb, ob = b.step(sb, inputs)
            for name in a.states:
                observe('state:' + name, na[name], nb[name])
                observe('next:' + name, na[name], nb[name])
            for name in set(oa) & set(ob):
                if name.startswith('guard:'):
                    observe(name, oa[name] != 0, ob[name] != 0)
                elif name.startswith(('effective:', 'action:', 'hold:', 'reset:', 'reset_update:')):
                    observe(name, oa[name], ob[name])
            # Raw payload differences are not automatically observable when valid is low.
            ea, eb = a.environment(na, inputs), b.environment(nb, inputs)
            observation_pairs = [(ea, eb)]
            if step == 0:
                initial_ea = a.environment(initial_a, prefix) if prefix is not None else oa
                initial_eb = b.environment(initial_b, prefix) if prefix is not None else ob
                observation_pairs.insert(0, (initial_ea, initial_eb))
            for ea, eb in observation_pairs:
                for port in current.ports:
                    if port.direction == 'output' and port.name in ea and port.name in eb:
                        observe('raw_output:' + port.name, ea[port.name], eb[port.name])
                if output_protocol:
                    if not output_protocol.get('evidence_ids'):
                        raise Unsupported('output protocol has no source evidence')
                    for name in output_protocol.get('always_valid', []):
                        observe('output:' + name, ea[name], eb[name])
                    for channel in output_protocol.get('channels', []):
                        valid = channel['valid']
                        va, vb = ea[valid] != 0, eb[valid] != 0
                        payload = z3.Or(*(ea[name] != eb[name] for name in channel.get('payload', [])))
                        different = z3.Or(va != vb, z3.And(va, vb, payload))
                        observe('output:' + valid, different, z3.BoolVal(False))
            frames.append((inputs, na, nb))
            sa, sb = na, nb
        feasible, reason, _, _ = _solve(solver, z3.BoolVal(True), budget_ms)
        if feasible != 'sat':
            return dict(status='invalid_environment' if feasible == 'unsat' else 'unknown',
                        reason=reason or 'no legal bounded execution', queries=[], horizon=horizon)
        results = []
        for name, terms in differences.items():
            condition = z3.Or(*terms)
            status, reason, solution, elapsed = _solve(solver, condition, budget_ms)
            result = unknown(assumption_id, alternative_id, 'bounded', reason or '', status)
            result.update(observable=name, horizon=horizon, budget_ms=budget_ms, elapsed_ms=elapsed,
                          visibility='G2', tool_version=z3.get_version_string(),
                          scope=dict(semantics='two_state', horizon=horizon, observation_domain=a.observation_domain,
                              reset_type=a.domain.reset_type, inter_edge_reset_pulses=False,
                              initialization='reset_prefix' if prefix else 'explicit_initialization'),
                          query_hash=digest(solver.sexpr()+condition.sexpr()))
            if solution is not None:
                result['witness'] = dict(reset_prefix=_model_values(solution, prefix) if prefix is not None else None,
                    initial_A=_model_values(solution, initial_a), initial_B=_model_values(solution, initial_b),
                    frames=[dict(inputs=_model_values(solution, inp), next_A=_model_values(solution, aa), next_B=_model_values(solution, bb)) for inp, aa, bb in frames])
            results.append(result)
        return dict(status='completed', horizon=horizon, queries=results,
                    negative_scope=f'no difference within {horizon} steps only')
    except (Unsupported, ImportError, KeyError) as exc:
        return dict(status='unknown', reason=str(exc), queries=[], horizon=horizon)
