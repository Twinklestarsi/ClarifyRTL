"""Emission-time spans. Tagged lines survive chunk concatenation unchanged."""
import hashlib


class MappedLine(str):
    def __new__(cls, text, ir_ids, role):
        obj = super().__new__(cls, text)
        obj.ir_ids, obj.role = list(ir_ids), role
        return obj


def tagged(text, *ir_ids, role):
    return MappedLine(text, ir_ids, role)


def collect(lines, rtl):
    rtl_hash = hashlib.sha256(rtl.encode('utf-8')).hexdigest()
    regions = []
    for number, line in enumerate(lines, 1):
        if isinstance(line, MappedLine):
            regions.append(dict(region_id=f'RTL:{number}:{line.role}', role=line.role,
                                semantic_role=line.role, ir_ids=line.ir_ids,
                                line_start=number, line_end=number,
                                precision='exact_emission', rtl_hash=rtl_hash))
    return dict(schema_version='c1-source-map-1', rtl_hash=rtl_hash, regions=regions,
                identity_scope='current_revision',
                unsupported=['memory_semantics', 'cdc_semantics', 'external_instances'])
