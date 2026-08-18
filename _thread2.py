import ast
import pathlib

p = pathlib.Path("jobs/paper_trading.py")
s = p.read_text()

# 1A — an authoritative routing hint, kept apart from the display fallback.
old_hint = '''            "asset_class": s.asset_class or ("Futures" if is_futures else "Equity"),'''
assert old_hint in s, "asset_class fallback not found"
s = s.replace(old_hint, old_hint + '''
            # ROUTING TRUTH, DELIBERATELY NOT THE DISPLAY FALLBACK ABOVE.
            # That fallback labels anything non-futures "Equity", which is
            # harmless for a label and fatal as routing identity: an unknown
            # crypto symbol would freeze as EQUITY_SPOT instead of honestly
            # resolving to UNRESOLVED. Only a STORED class, or a genuine
            # futures determination, is authoritative here.
            "routing_asset_class_hint": (
                s.asset_class or ("Futures" if is_futures else None)),''', 1)

# Resolve the frozen identity ONCE, before any terminal branch can fire.
old_edge = "        edge, edge_role = None, DO_CONST.EDGE_NOT_EVALUATED"
assert old_edge in s, "edge init not found"
s = s.replace(old_edge, '''        # THE FROZEN T0 IDENTITY, resolved ONCE before any branch can end
        # this candidate. Every route below carries this same object, so an
        # AI rejection now records the product it was about exactly as a
        # settled trade does — which is the whole defect being closed.
        identity = RI.resolve_execution_identity(
            sym, sig.get("routing_asset_class_hint"), signal=sig)

''' + old_edge, 1)

# Carry it into every funnel-owned terminal route.
reps = [
    ('''                sig, decision="ABSTAIN", reason=DF.ALREADY_OPEN,
                decision_price=None, edge=edge, edge_gate_role=edge_role)''',
     '''                sig, decision="ABSTAIN", reason=DF.ALREADY_OPEN,
                decision_price=None, edge=edge, edge_gate_role=edge_role,
                routing_identity=identity)'''),
    ('''                decision_price=None, venue_failure=True,
                edge=edge, edge_gate_role=edge_role)''',
     '''                decision_price=None, venue_failure=True,
                edge=edge, edge_gate_role=edge_role,
                routing_identity=identity)'''),
    ('''                decision_price=price, edge=edge, edge_gate_role=edge_role,
                gates={"ai_entry_review": "FAIL"})''',
     '''                decision_price=price, edge=edge, edge_gate_role=edge_role,
                routing_identity=identity,
                gates={"ai_entry_review": "FAIL"})'''),
]
for old, new in reps:
    assert old in s, f"call site not found: {old[:60]}"
    s = s.replace(old, new, 1)

s = s.replace("    from lib import decision_observation as DO_CONST",
              "    from lib import decision_observation as DO_CONST\n"
              "    from lib import routing_identity as RI", 1)

p.write_text(s)
ast.parse(s)
print("paper_trading threaded cleanly")
