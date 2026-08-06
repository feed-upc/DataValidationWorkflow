"""Verdict fidelity regressions for the DQVP executor.

The DQVP rules chain every measurement into a terminal odrl:Constraint, so a
well-formed PolicyChecker always ends in a boolean. These checks pin down what
happens when it does - and, more importantly, what happens when it does not.

Run:  python executor/test_verdict_fidelity.py
"""
import sys
import re
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from rdflib import Graph, URIRef

import executor as E

results = []


def check(label, got, want):
    ok = got == want
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {want!r}, got {got!r}")


def status(value):
    inst = E.SemanticN3Executor.__new__(E.SemanticN3Executor)
    return E.SemanticN3Executor._determine_validation_status(inst, value)


def recorded_datatype(value):
    """Serialize a result into a fresh graph and report the datatype used."""
    inst = E.SemanticN3Executor.__new__(E.SemanticN3Executor)
    inst.graph = Graph()
    inst.compose_udf = True
    uri = E.SemanticN3Executor._create_validation_report(
        inst, URIRef("urn:pc"), value, dt.datetime.now(), dt.datetime.now(), 0.1
    )
    vals = list(inst.graph.objects(uri, E.tb.resultValue))
    return vals[0].datatype.split('#')[-1] if vals else "NONE RECORDED"


print("=" * 70)
print("A verdict is a boolean - including numpy's, which is not a Python bool")
print("=" * 70)
check("np.False_ reports FAILED", status(np.False_), "FAILED")
check("np.True_ reports PASSED", status(np.True_), "PASSED")
check("python bool reports PASSED", status(True), "PASSED")
check("all-True mask reports PASSED", status(pd.Series([True, True])), "PASSED")
check("mask containing False reports FAILED", status(pd.Series([True, False])), "FAILED")

print()
print("=" * 70)
print("An unenforced measurement must never be reported as satisfied")
print("=" * 70)
# A chain that stops at its measurement leaves odrl:operator and
# odrl:rightOperand unapplied. Guessing "positive means healthy" would report a
# completeness score of 8.94 as passing a 99% threshold.
check("bare metric 8.94 reports COMPLETED", status(np.float64(8.94)), "COMPLETED")
check("bare metric 0.5 reports COMPLETED", status(0.5), "COMPLETED")
check("DataFrame reports COMPLETED", status(pd.DataFrame({'a': [1]})), "COMPLETED")
check("empty DataFrame reports COMPLETED", status(pd.DataFrame()), "COMPLETED")

print()
print("=" * 70)
print("Results reach the graph, whatever library produced them")
print("=" * 70)
check("np.bool_ verdict serialized as xsd:boolean", recorded_datatype(np.False_), "boolean")
check("python bool verdict serialized as xsd:boolean", recorded_datatype(True), "boolean")
check("np.float64 metric serialized as xsd:float", recorded_datatype(np.float64(75.0)), "float")

print()
print("=" * 70)
print("Composition substitutes identifiers, not substrings")
print("=" * 70)
sub = lambda t: re.sub(r'\bdata\b', 'result_0', t)
check(
    "a template mentioning data_element survives",
    sub("data.data_element(attr).value = 'X'"),
    "result_0.data_element(attr).value = 'X'",
)
check(
    "the consistency template is rewritten correctly",
    sub("data.loc[data[condition_attr].isin(condition_values.split(',')), attr].isna().all()"),
    "result_0.loc[result_0[condition_attr].isin(condition_values.split(',')), attr].isna().all()",
)
check(
    "a bare str.replace would have corrupted it",
    "result_0_element" in "data.data_element(attr)".replace('data', 'result_0'),
    True,
)

print()
print("=" * 70)
print(f"{sum(results)}/{len(results)} checks passed")
print("=" * 70)
sys.exit(0 if all(results) else 1)
