"""Same scheduling/reset decisions as the deterministic compiler, or unknown."""
from Compiler.rtl_compiler.backend import _derived_expression, _derived_width, _expr
from Compiler.rtl_compiler.lowering import build_processes, ordered_for_target
from .expressions import Unsupported, parse, evaluate, resize


class TransitionSystem:
    def __init__(self, model):
        self.model = model
        if len(model.clock_domains) != 1 or model.resources or model.cdc or model.parameters:
            raise Unsupported('requires one clock, concrete widths, no resources/CDC/parameters')
        self.domain = model.clock_domains[0]
        if self.domain.reset_type not in ('synchronous', 'sync', 'asynchronous', 'async'):
            raise Unsupported('unsupported reset event type')
        # The bounded observation domain is active-clock samples. An asserted
        # async reset dominates that sample, exactly as in emitted RTL. This
        # does not prove behavior for reset pulses wholly between clock edges.
        self.observation_domain = 'active_clock_samples'
        self.widths = {}
        for obj in list(model.ports) + list(model.states):
            width = obj.width
            if isinstance(width, str) and width.isdigit():
                width = int(width)
            if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 4096:
                raise Unsupported(f'non-concrete width: {obj.name}')
            signed = getattr(obj, 'signed', False) or ('signed' in getattr(obj, 'datatype', '') and 'unsigned' not in getattr(obj, 'datatype', ''))
            if signed or getattr(obj, 'type', '') in ('memory', 'ram'):
                raise Unsupported('signed arithmetic or memory is outside first-version semantics')
            if obj.name in self.widths and self.widths[obj.name] != width:
                raise Unsupported('state/output effective width mismatch')
            self.widths[obj.name] = width
        self.states = list(model.state_map())
        self.inputs = [p.name for p in model.ports if p.direction == 'input' and p.name != self.domain.clock]
        self.derived = {str(s['name']): (parse(_derived_expression(s)), _derived_width(model, s)) for s in model.derived_signals}
        for name, (_, width) in self.derived.items():
            if not isinstance(width, int) or width <= 0:
                raise Unsupported('derived width not concrete')
            if name in self.widths and self.widths[name] != width:
                raise Unsupported('derived/output effective width mismatch')
            self.widths[name] = width
        self.writers = {}
        for process in build_processes(model):
            for target in self.states:
                names = ordered_for_target(model, process, target)
                # A reset-only target is still emitted and receives hold/reset.
                present = any(a.base_target == target for n in process.transitions for a in model.transition_map()[n].actions)
                if not present:
                    continue
                if target in self.writers:
                    raise Unsupported('multiple processes write a state')
                self.writers[target] = names
        for state in self.states:
            if state not in self.writers:
                raise Unsupported('state without emitted clocked process')
        for transition in model.transitions:
            for action in transition.actions + transition.else_actions:
                if action.target != action.base_target:
                    raise Unsupported('partial target updates need interval/address semantics')
            if transition.else_actions and len(self.writers.get(transition.actions[0].target if transition.actions else '', [])) > 1:
                raise Unsupported('else-actions with competing writers')
        # Validate now, not only when a solver happens to visit a branch.
        self.step(self.variables('validate_s', self.states), self.variables('validate_i', self.inputs))

    def variables(self, prefix, names):
        import z3
        return {name: z3.BitVec(f'{prefix}_{name}', self.widths[name]) for name in names}

    def value(self, text, env, width=None):
        normalized = _expr(text)
        if width is None and normalized in ('0','1'):
            normalized = "1'b" + normalized
        return evaluate(parse(normalized), env, self.widths, width)

    def environment(self, state, inputs):
        env = dict(state, **inputs)
        pending = dict(self.derived)
        combinational = [t for t in self.model.transitions if t.transition_type == 'combinational']
        for transition in combinational:
            for action in transition.actions:
                if action.target in pending:
                    raise Unsupported('multiple derived/combinational drivers')
                other = next((a for a in transition.else_actions if a.target == action.target), None)
                if transition.guard in ('1', "1'b1", 'true', 'True'):
                    text = action.expression
                elif other:
                    text = f'({transition.guard}) ? ({action.expression}) : ({other.expression})'
                else:
                    raise Unsupported('conditional combinational update without else')
                pending[action.target] = (parse(text), self.widths.get(action.target))
        while pending:
            ready = [name for name, (ast, _) in pending.items() if ast.symbols() <= env.keys()]
            if not ready:
                raise Unsupported('combinational loop or unbound expression symbol')
            for name in ready:
                ast, width = pending.pop(name)
                if width is None or name in state:
                    raise Unsupported('untyped combinational target or state driver')
                env[name] = resize(evaluate(ast, env, self.widths, width), width)
        return env

    def step(self, state, inputs):
        import z3
        env = self.environment(state, inputs)
        observations = dict(env)
        result = dict(state)
        transitions = self.model.transition_map()
        for target, names in self.writers.items():
            active_reset = z3.BoolVal(False)
            if self.domain.reset:
                active_reset = inputs[self.domain.reset] != 0
                if self.domain.reset_polarity in ('active_low', 'low'):
                    active_reset = z3.Not(active_reset)
                observations[f'reset:{self.domain.name}'] = active_reset
            selected = z3.BoolVal(False)
            for name in names:
                transition = transitions[name]
                raw_guard = self.value(transition.guard, env) != 0
                effective = z3.And(z3.Not(active_reset), z3.Not(selected), raw_guard)
                observations[f'effective:{name}:{target}'] = effective
                selected = z3.Or(selected, raw_guard)
                for index, action in enumerate(transition.actions):
                    if action.target == target:
                        contribution = resize(self.value(action.expression, env, self.widths[target]), self.widths[target])
                        observations[f'action:{name}:{index}'] = z3.Concat(
                            z3.If(effective,z3.BitVecVal(1,1),z3.BitVecVal(0,1)),
                            z3.If(effective,contribution,z3.BitVecVal(0,self.widths[target])))
            observations[f'hold:{target}'] = z3.And(z3.Not(active_reset), z3.Not(selected))
            width = self.widths[target]
            value = state[target]
            for name in reversed(names):
                transition = transitions[name]
                actions = [a for a in transition.actions if a.target == target]
                if len(actions) != 1:
                    raise Unsupported('multiple assignments to same full target')
                guard = self.value(transition.guard, env)
                yes = resize(self.value(actions[0].expression, env, width), width)
                no = next((a for a in transition.else_actions if a.target == target), None)
                if no:
                    value = resize(self.value(no.expression, env, width), width)
                value = z3.If(guard != 0, yes, value)
                observations[f'guard:{name}'] = guard
            if self.domain.reset:
                active = inputs[self.domain.reset] != 0
                if self.domain.reset_polarity in ('active_low', 'low'):
                    active = z3.Not(active)
                reset_value = resize(self.value(self.model.state_map()[target].reset_value, env, width), width)
                value = z3.If(active, reset_value, value)
                observations[f'reset_update:{target}'] = z3.If(active,reset_value,z3.BitVecVal(0,width))
            result[target] = value
            observations[f'next:{target}'] = value
        return result, observations

    def initial_pair(self, other):
        """Execute a common reset event; reset-value variants may then diverge."""
        import z3
        if self.domain != other.domain:
            raise Unsupported('clock/reset contract changed')
        before = self.variables('initial', self.states)
        inputs = self.variables('reset_prefix', self.inputs)
        if self.domain.reset:
            inputs[self.domain.reset] = z3.BitVecVal(0 if self.domain.reset_polarity in ('active_low', 'low') else 1, self.widths[self.domain.reset])
            return self.step(before, inputs)[0], other.step(before, inputs)[0], inputs
        values = {}
        for state in self.model.states:
            other_state = other.model.state_map()[state.name]
            if state.initial_value is None or other_state.initial_value is None:
                raise Unsupported('reachable query requires explicit reset or initialization')
            values[state.name] = resize(self.value(state.initial_value, {}, self.widths[state.name]), self.widths[state.name])
        values_b = {s.name: resize(other.value(s.initial_value, {}, self.widths[s.name]), self.widths[s.name]) for s in other.model.states}
        return values, values_b, None
