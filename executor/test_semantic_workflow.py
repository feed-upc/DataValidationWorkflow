"""
Test Semantic N3 Workflow
=========================
Complete end-to-end test of semantic approach:
1. Planner generates PolicyChecker with semantic properties
2. N3 binding rules bind to parameters
3. Executor executes operations

Tests both p6 (Timeliness) and p2 (Quality) policies.
"""

import subprocess
import tempfile
from pathlib import Path
from rdflib import Graph, Namespace, URIRef, Literal, RDF
import pandas as pd
from datetime import datetime
import sys

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).parent))

from executor_semantic import SemanticN3Executor, load_policy_checker

# Namespaces
tb = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
abox = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#")
odrl = Namespace("http://www.w3.org/ns/odrl/2/")
dqv = Namespace("http://www.w3.org/ns/dqv#")


def apply_binding_rules(pc_graph: Graph) -> Graph:
    """
    Apply N3 parameter binding rules to PolicyChecker.
    This simulates what would happen if EYE reasoner applied the rules.
    """
    print("\n" + "="*60)
    print("Applying N3 Parameter Binding Rules")
    print("="*60)
    
    # Write PolicyChecker to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.n3', delete=False) as f:
        pc_graph.serialize(f.name, format='n3')
        pc_file = f.name
    
    # Write query
    query = """
@prefix : <http://www.w3.org/2000/10/swap/log#> .
{ ?s ?p ?o } => { ?s ?p ?o } .
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.n3', delete=False) as f:
        f.write(query)
        query_file = f.name
    
    # Path to binding rules
    rules_file = Path(__file__).parent / "parameter_binding_semantic.n3"
    
    # Run EYE reasoner
    cmd = ['eye', pc_file, str(rules_file), '--query', query_file, '--nope']
    
    print(f"Running: {' '.join(cmd)}")
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    
    if result.returncode != 0:
        print(f"ERROR: {result.stderr}")
        raise RuntimeError(f"EYE failed: {result.stderr}")
    
    # Parse result
    result_graph = Graph()
    result_graph.parse(data=result.stdout, format='n3')
    
    # Clean up
    Path(pc_file).unlink()
    Path(query_file).unlink()
    
    print(f"✓ Generated {len(result_graph)} triples (with bindings)")
    
    # Show some bound parameters
    print("\nBound Parameters:")
    for s in result_graph.subjects(tb.boundParameter, None):
        op_type = result_graph.value(s, tb.hasAbstract)
        print(f"  {op_type}:")
        for bp in result_graph.objects(s, tb.boundParameter):
            name = result_graph.value(bp, tb.name)
            value = result_graph.value(bp, tb.value)
            print(f"    {name} = {value}")
    
    return result_graph


def test_policy(pc_uri: URIRef, policy_name: str, exec_graph: Graph):
    """Test a single policy"""
    print("\n" + "="*60)
    print(f"Testing {policy_name}")
    print("="*60)
    
    # Create executor (uses composed UDF by default, with graph annotation)
    metadata_file = str(Path(__file__).parent / "code_metadata_with_roles.json")
    executor = SemanticN3Executor(exec_graph, metadata_file, annotate_graph=True)
    
    # Execute
    try:
        result, report_uri = executor.execute(pc_uri)
        
        print(f"\n{'='*60}")
        print(f"✓ {policy_name} PASSED")
        print(f"{'='*60}")
        print(f"Result: {result}")
        
        if isinstance(result, pd.DataFrame):
            print(f"  Shape: {result.shape}")
            if len(result) > 0:
                print(f"  Sample:")
                print(result.head())
        
        # Show validation report metadata
        if report_uri:
            print(f"\nValidation Report: {report_uri}")
            status = exec_graph.value(report_uri, tb.validationStatus)
            duration = exec_graph.value(report_uri, tb.executionDuration)
            exec_mode = exec_graph.value(report_uri, tb.executionMode)
            print(f"  Status: {status}")
            print(f"  Duration: {duration} seconds")
            print(f"  Execution Mode: {exec_mode}")
        
        return result, report_uri
        
    except Exception as e:
        print(f"\n{'='*60}")
        print(f"✗ {policy_name} FAILED")
        print(f"{'='*60}")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        raise


def main():
    """Run complete semantic workflow test"""
    print("\n" + "="*80)
    print("SEMANTIC N3 WORKFLOW TEST")
    print("="*80)
    
    # Step 1: Load PolicyChecker from planner output
    pc_file = str(Path(__file__).parent.parent / "planner" / "policy_checker_UPENN_GBM_clinical_info_v21_timestamp_semantic.ttl")
    
    print(f"\nStep 1: Loading PolicyChecker from {pc_file}")
    pc_graph, _ = load_policy_checker(pc_file)
    print(f"  Loaded {len(pc_graph)} triples")
    
    # Find both policy checkers
    p6_uri = None
    p2_uri = None
    for s in pc_graph.subjects(RDF.type, tb.PolicyChecker):
        policy = pc_graph.value(s, tb.accordingTo)
        # Check for DQRP1 (Timeliness)
        if policy == abox.DQRP1 or str(policy).endswith("DQRP1") or str(policy).endswith("p6"):
            p6_uri = s
        # Check for DQRP2 (Completeness)
        elif policy == abox.DQRP2 or str(policy).endswith("DQRP2") or str(policy).endswith("p2"):
            p2_uri = s
    
    print(f"  Found p6 PolicyChecker: {p6_uri}")
    print(f"  Found p2 PolicyChecker: {p2_uri}")
    
    # Step 2: Apply N3 binding rules
    print(f"\nStep 2: Applying N3 parameter binding rules")
    exec_graph = apply_binding_rules(pc_graph)
    
    # Debug: Check implementations
    print(f"\nImplementations bound:")
    for s in exec_graph.subjects(tb.hasImplementation, None):
        op_type = exec_graph.value(s, tb.hasAbstract)
        impl = exec_graph.value(s, tb.hasImplementation)
        print(f"  {op_type} -> {impl}")
    
    # Step 3: Execute p6 (Timeliness)
    print(f"\nStep 3: Executing p6 (Timeliness)")
    p6_result, p6_report = test_policy(p6_uri, "p6 (Timeliness)", exec_graph)
    
    # Step 4: Execute p2 (Quality)
    print(f"\nStep 4: Executing p2 (Quality)")
    p2_result, p2_report = test_policy(p2_uri, "p2 (Quality)", exec_graph)
    
    # Step 5: Save annotated graph with validation reports
    output_file = str(Path(__file__).parent.parent / "planner" / "policy_checker_UPENN_GBM_clinical_info_v21_timestamp_with_reports.ttl")
    print(f"\nStep 5: Saving annotated graph with validation reports")
    
    # Get executor instance to save (create temporary one)
    metadata_file = str(Path(__file__).parent / "code_metadata_with_roles.json")
    temp_executor = SemanticN3Executor(exec_graph, metadata_file, annotate_graph=True)
    temp_executor.save_annotated_graph(output_file)
    
    # Step 6: Merge validation reports back to SDM
    print(f"\nStep 6: Merging validation reports back to SDM")
    from executor_semantic import merge_validation_reports_to_sdm
    
    root_dir = Path(__file__).parent.parent.parent.parent
    sdm_file = str(root_dir / "FederatedComputationalGovernance/SemanticDataModel/sdm.ttl")
    output_sdm = str(root_dir / "FederatedComputationalGovernance/SemanticDataModel/sdm_with_validation_reports.ttl")
    
    merged_sdm = merge_validation_reports_to_sdm(exec_graph, sdm_file, output_sdm)
    
    # Summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print("✓ Planner generated semantic PolicyCheckers")
    print("✓ N3 binding rules applied successfully")
    print("✓ p6 (Timeliness) executed successfully")
    print("✓ p2 (Quality) executed successfully")
    print(f"✓ Annotated graph saved with validation reports")
    print(f"  Report URIs: {p6_report}, {p2_report}")
    print(f"✓ Validation reports merged back to SDM")
    print(f"  SDM file: {output_sdm}")
    print("\nSEMANTIC APPROACH WORKS END-TO-END! 🎉")
    print("="*80)


if __name__ == "__main__":
    main()
