"""Check confirmed priority contracts against normalized compiler scheduling."""
from .candidates import scan_choices


def check_conformance(model, contract, requirements=()):
    choices = scan_choices(model, list(requirements))
    results = []
    for constraint in (contract or {}).get('constraints', []):
        result = dict(constraint_id=constraint['id'], status='unknown', evidence_ids=constraint.get('evidence_ids', []))
        if constraint['kind'] == 'priority':
            high, low = constraint['higher_operation'], constraint['lower_operation']
            matches = [a for a in choices if a['kind'] == 'priority' and set(a['operations']) == {high, low}]
            if len(matches) == 1 and matches[0]['binding_state'] == 'verified':
                choice = matches[0]
                result.update(status='conformant' if choice['operations'][0] == high else 'violated',
                              assumption_id=choice['id'], bindings=choice['bindings'], scope='compiler_schedule')
            else:
                result['reason'] = 'operation binding ambiguous, nonadjacent or masked; semantic conformance unknown'
        else:
            result['reason'] = 'unsupported contract constraint kind'
        results.append(result)
    status = 'violated' if any(r['status']=='violated' for r in results) else 'unknown' if any(r['status']=='unknown' for r in results) else 'conformant'
    return dict(status=status, results=results, constraints_checked=len(results))
