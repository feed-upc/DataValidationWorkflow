"""
Semantic N3-Based Executor
===========================

This executor works with PolicyCheckers that use SEMANTIC properties instead of flat hasInput.

Key Changes:
    - Reads semantic properties (tb:hasFilePath, tb:hasTimestampAttribute, etc.)
    - Handles structured odrl:constraint blocks
    - No ambiguity - property names encode parameter roles
    - Scalable to multi-parameter operations

Author: acraf
Version: 2.0.0-semantic
"""

import pandas as pd
from datetime import datetime
from pathlib import Path
import json
import time
import hashlib
from typing import Any, Dict, Optional, Tuple
from rdflib import Graph, Namespace, URIRef, Literal, RDF, BNode, XSD

# Namespaces
tb = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
abox = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#")
odrl = Namespace("http://www.w3.org/ns/odrl/2/")
dqv = Namespace("http://www.w3.org/ns/dqv#")
prov = Namespace("http://www.w3.org/ns/prov#")


class CodeMetadataLoader:
    """Loads code templates from JSON (same as before)"""
    
    def __init__(self, metadata_file: str):
        with open(metadata_file, 'r') as f:
            self.metadata = json.load(f)
        
        # Get context for namespace expansion
        self.context = self.metadata.get('@context', {})
        
        # Index by implementation URI (expand prefixes)
        self.implementations = {}
        for impl in self.metadata.get('@graph', []):
            impl_id = impl.get('@id')
            if impl_id:
                # Expand prefix if present
                expanded_id = self._expand_prefix(impl_id)
                self.implementations[expanded_id] = impl
    
    def _expand_prefix(self, uri_or_curie: str) -> str:
        """Expand CURIE (ab:Imp1) to full URI"""
        if ':' in uri_or_curie and not uri_or_curie.startswith('http'):
            prefix, local = uri_or_curie.split(':', 1)
            if prefix in self.context:
                return self.context[prefix] + local
        return uri_or_curie
    
    def get_code_template(self, impl_uri: URIRef) -> str:
        """Get code template for implementation"""
        impl_id = str(impl_uri)
        impl = self.implementations.get(impl_id)
        
        if not impl:
            raise ValueError(f"Implementation not found: {impl_id}")
        
        code_blocks = impl.get('tb:hasCode', [])
        if not code_blocks:
            raise ValueError(f"No code found for {impl_id}")
        
        return code_blocks[0].get('tb:code')
    
    def get_dependencies(self, impl_uri: URIRef) -> list:
        """Get dependencies for implementation"""
        impl_id = str(impl_uri)
        impl = self.implementations.get(impl_id)
        
        if not impl:
            return []
        
        deps = impl.get('tb:dependsOn', [])
        return [d.get('tb:name') for d in deps]


class SemanticN3Executor:
    """
    Executor for semantic PolicyCheckers.
    
    Reads SEMANTIC properties instead of flat hasInput:
        - tb:hasFilePath -> parameter "p"
        - tb:hasPreviousResult -> parameter "data"
        - tb:hasTimestampAttribute -> parameter "ts_attr"
        - tb:hasTargetAttribute -> parameter "attr"
        - odrl:constraint/odrl:rightOperand -> parameter "threshold"
        - odrl:constraint/odrl:operator -> parameter "operator"
    """
    
    def __init__(self, pc_graph: Graph, code_metadata_file: str, compose_udf: bool = True, annotate_graph: bool = True):
        self.graph = pc_graph
        self.code_metadata = CodeMetadataLoader(code_metadata_file)
        self.operations_cache = {}
        self.compose_udf = compose_udf  # Flag to use composed UDF approach (default: True)
        self.annotate_graph = annotate_graph  # Flag to annotate graph with execution metadata
        
        # Bind additional namespaces
        self.graph.bind('prov', prov)
        self.graph.bind('xsd', XSD)
    
    def execute(self, pc_uri: URIRef) -> Tuple[Any, Optional[URIRef]]:
        """Execute a PolicyChecker and optionally annotate with execution metadata
        
        Returns:
            Tuple of (result, validation_report_uri)
        """
        print(f"\n{'='*60}")
        print(f"Executing PolicyChecker: {pc_uri}")
        print(f"Execution Mode: COMPOSED UDF")
        print(f"{'='*60}\n")
        
        # Find first operation
        first_op = self.graph.value(pc_uri, tb.nextStep)
        if not first_op:
            raise ValueError(f"No operations found for PolicyChecker {pc_uri}")
        
        # Track execution time
        start_time = time.time()
        execution_start = datetime.now()
        
        # Execute operation chain using composed UDF approach
        result = self._execute_chain_composed(first_op)
        
        # Calculate execution time
        end_time = time.time()
        execution_end = datetime.now()
        execution_duration = end_time - start_time
        
        print(f"{'='*60}\n")
        print(f"⏱️  Execution time: {execution_duration:.4f} seconds\n")
        
        # Create validation report if annotation is enabled
        report_uri = None
        if self.annotate_graph:
            report_uri = self._create_validation_report(
                pc_uri=pc_uri,
                result=result,
                execution_start=execution_start,
                execution_end=execution_end,
                execution_duration=execution_duration
            )
            print(f"📊 Validation report created: {report_uri}\n")
        
        return result, report_uri
    
    def _execute_chain(self, op_uri: URIRef, previous_result: Any = None) -> Any:
        """Execute operation chain - Option 1: Sequential (current)"""
        current_op = op_uri
        result = previous_result
        op_count = 0
        
        while current_op:
            op_count += 1
            
            # Execute current operation
            result = self.execute_operation(current_op, result, op_count)
            
            # Check if terminal
            is_terminal = self.graph.value(current_op, tb.isTerminal)
            if is_terminal and is_terminal.toPython():
                print(f"  ✓ Terminal operation reached\n")
                break
            
            # Get next operation
            current_op = self.graph.value(current_op, tb.nextStep)
        
        return result
    
    def _execute_chain_composed(self, op_uri: URIRef, previous_result: Any = None) -> Any:
        """Execute operation chain - Option 2: Compose all UDFs then execute"""
        
        # Step 1: Collect all operations and compose UDF
        print(f"\n{'='*60}")
        print("COMPOSING UDF PIPELINE")
        print(f"{'='*60}\n")
        
        operations = []
        current_op = op_uri
        op_count = 0
        
        while current_op:
            op_count += 1
            op_type = self.graph.value(current_op, tb.hasAbstract)
            impl_uri = self.graph.value(current_op, tb.hasImplementation)
            
            if not impl_uri:
                impl_uri = self._infer_implementation(op_type)
            
            # Get bindings (placeholder for now, will resolve at execution)
            bindings_template = self.get_semantic_bindings(current_op, None)
            code_template = self.code_metadata.get_code_template(impl_uri)
            
            operations.append({
                'num': op_count,
                'uri': current_op,
                'type': op_type,
                'impl': impl_uri,
                'bindings_template': bindings_template,
                'code_template': code_template
            })
            
            print(f"  Step {op_count}: {op_type}")
            print(f"    Template: {code_template}")
            
            # Check if terminal
            is_terminal = self.graph.value(current_op, tb.isTerminal)
            if is_terminal and is_terminal.toPython():
                break
            
            current_op = self.graph.value(current_op, tb.nextStep)
        
        # Step 2: Compose into single function
        print(f"\n{'='*60}")
        print("COMPOSED UDF:")
        print(f"{'='*60}")
        
        composed_code = "def composed_pipeline(initial_data=None):\n"
        
        for i, op in enumerate(operations):
            if i == 0:
                # First operation
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    result_{i} = {op['code_template']}\n\n"
            else:
                # Subsequent operations - replace 'data' with previous result
                code = op['code_template'].replace('data', f'result_{i-1}')
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    result_{i} = {code}\n\n"
        
        composed_code += f"    return result_{len(operations)-1}\n"
        
        print(composed_code)
        print(f"{'='*60}\n")
        
        # Step 3: Execute composed function
        print("EXECUTING COMPOSED UDF...")
        
        # Prepare execution environment
        exec_env = {
            'pandas': pd,
            'pd': pd,
            'datetime': datetime,
        }
        
        # Add first operation's parameters
        first_bindings = self.get_semantic_bindings(operations[0]['uri'], previous_result)
        exec_env.update(first_bindings)
        
        # Add other static parameters
        for op in operations[1:]:
            bindings = self.get_semantic_bindings(op['uri'], None)
            for k, v in bindings.items():
                if k != 'data':  # Skip 'data' as it comes from pipeline
                    exec_env[k] = v
        
        # Execute composed function
        exec(composed_code, exec_env)
        result = exec_env['composed_pipeline'](previous_result)
        
        print(f"✓ Composed UDF executed successfully\n")
        
        return result
    
    def execute_operation(self, op_uri: URIRef, previous_result: Any, op_num: int) -> Any:
        """Execute a single operation using semantic properties"""
        
        # Get operation type
        op_type = self.graph.value(op_uri, tb.hasAbstract)
        print(f"  Operation {op_num}/{self._count_operations(op_uri)}:")
        print(f"    Executing: {op_type}")
        
        # Get implementation
        impl_uri = self.graph.value(op_uri, tb.hasImplementation)
        if not impl_uri:
            # Try to infer from abstract type
            impl_uri = self._infer_implementation(op_type)
        
        print(f"      Implementation: {impl_uri}")
        
        # Get bound parameters using SEMANTIC properties
        bindings = self.get_semantic_bindings(op_uri, previous_result)
        
        # Print parameter bindings
        for param_name, param_value in bindings.items():
            value_str = str(param_value)
            if isinstance(param_value, pd.DataFrame):
                value_str = f"DataFrame({param_value.shape[0]} rows × {param_value.shape[1]} cols)"
            elif isinstance(param_value, str) and len(value_str) > 50:
                value_str = value_str[:50] + "..."
            print(f"      {param_name} = {value_str}")
        
        # Get code template
        code_template = self.code_metadata.get_code_template(impl_uri)
        print(f"      Template: {code_template[:60]}...")
        
        # Substitute parameters
        code = self.substitute_parameters(code_template, bindings)
        print(f"      Code: {code[:80]}...")
        
        # Show complete generated UDF
        print(f"\n      Generated UDF:")
        print(f"      {'─'*50}")
        for line in code.split('\n'):
            print(f"      │ {line}")
        print(f"      {'─'*50}\n")
        
        # Execute
        local_vars = self._prepare_execution_env(impl_uri, previous_result)
        local_vars.update(bindings)
        
        try:
            exec(f"result = {code}", local_vars)
            result = local_vars['result']
            print(f"      ✓ Success")
            
            # Print result info
            if isinstance(result, pd.DataFrame):
                print(f"      Result: DataFrame({result.shape[0]} rows × {result.shape[1]} cols)")
            elif isinstance(result, bool):
                print(f"      Result: {result}")
            elif isinstance(result, (int, float)):
                print(f"      Result: {result}")
            else:
                print(f"      Result: {type(result).__name__}")
            
            return result
            
        except Exception as e:
            print(f"      ✗ FAILED: {e}")
            raise
    
    def get_semantic_bindings(self, op_uri: URIRef, previous_result: Any) -> Dict[str, Any]:
        """
        Extract parameter bindings from ROLE-ANNOTATED hasInput nodes.
        
        Each hasInput points to a blank node with:
            tb:value - The actual value
            tb:role - The semantic role ("filePath", "previousResult", "threshold", etc.)
        
        This approach keeps hasInput for analysis while using roles for unambiguous binding!
        """
        bindings = {}
        
        # Iterate through all hasInput nodes
        for input_node in self.graph.objects(op_uri, tb.hasInput):
            # Get role and value from the input node
            role = self.graph.value(input_node, tb.role)
            value = self.graph.value(input_node, tb.value)
            
            if not role or not value:
                continue
            
            role_str = str(role)
            
            # Map role to parameter name
            if role_str == "filePath":
                bindings['p'] = str(value)
            
            elif role_str == "previousResult":
                bindings['data'] = previous_result
            
            elif role_str == "timestampAttribute":
                bindings['ts_attr'] = self._extract_local_name(value)
            
            elif role_str == "targetAttribute":
                bindings['attr'] = self._extract_local_name(value)
            
            elif role_str == "threshold":
                bindings['threshold'] = value.toPython() if isinstance(value, Literal) else value
            
            elif role_str == "operator":
                bindings['operator'] = str(value)
        
        return bindings
    
    def _extract_local_name(self, uri: URIRef) -> str:
        """Extract local name from URI (e.g., abox:ts -> 'ts')"""
        uri_str = str(uri)
        if '#' in uri_str:
            return uri_str.split('#')[-1]
        elif '/' in uri_str:
            return uri_str.split('/')[-1]
        return uri_str
    
    def _count_operations(self, first_op: URIRef) -> int:
        """Count total operations in chain"""
        count = 0
        current = first_op
        visited = set()
        
        while current and current not in visited:
            count += 1
            visited.add(current)
            current = self.graph.value(current, tb.nextStep)
        
        return count
    
    def _infer_implementation(self, op_type: URIRef) -> URIRef:
        """Infer implementation from operation type"""
        # Simple mapping
        type_to_impl = {
            str(abox.LoadData): str(abox.Imp1),
            str(abox.CheckFreshness): str(abox.ImpCheckFreshness),
            str(abox.qM): str(abox.ImpQualityMetric),
            str(odrl.Constraint): str(abox.ImpConstraintLteq),
            str(dqv.completeness): str(abox.ImpQualityCompleteness),
            str(abox.CheckValidity): str(abox.ImpCheckValidity),
            str(abox.CheckConsistency): str(abox.ImpCheckConsistency),
        }
        
        impl_str = type_to_impl.get(str(op_type))
        if impl_str:
            return URIRef(impl_str)
        
        raise ValueError(f"Cannot infer implementation for {op_type}")
    
    def _prepare_execution_env(self, impl_uri: URIRef, previous_result: Any) -> Dict:
        """Prepare execution environment with dependencies"""
        env = {
            'pandas': pd,
            'pd': pd,
            'datetime': datetime,
        }
        
        # Add dependencies
        deps = self.code_metadata.get_dependencies(impl_uri)
        for dep in deps:
            if dep == 'pandas':
                env['pandas'] = pd
                env['pd'] = pd
            elif dep == 'datetime':
                env['datetime'] = datetime
        
        return env
    
    def substitute_parameters(self, code_template: str, bindings: Dict[str, Any]) -> str:
        """
        Substitute parameters into code template.
        Important: Don't use string replace - put values in local vars instead!
        """
        # For string values, we add them to the environment
        # For numeric values, we can safely substitute
        # For DataFrame/object values, they're already in local vars
        
        # Return code as-is - parameters will be in local_vars
        return code_template
    
    def _create_validation_report(self, pc_uri: URIRef, result: Any, 
                                   execution_start: datetime, execution_end: datetime,
                                   execution_duration: float) -> URIRef:
        """Create a validation report node in the graph with execution metadata
        
        Args:
            pc_uri: PolicyChecker URI
            result: Execution result
            execution_start: Start timestamp
            execution_end: End timestamp
            execution_duration: Duration in seconds
            
        Returns:
            URI of the created validation report
        """
        # Generate unique report URI
        timestamp_str = execution_start.strftime("%Y%m%d%H%M%S")
        report_hash = hashlib.md5(f"{pc_uri}{timestamp_str}".encode()).hexdigest()[:8]
        report_uri = URIRef(f"{abox}ValidationReport-{report_hash}")
        
        # Add report node
        self.graph.add((report_uri, RDF.type, tb.ValidationReport))
        self.graph.add((report_uri, prov.wasGeneratedBy, pc_uri))
        
        # Link PolicyChecker to report
        self.graph.add((pc_uri, tb.hasValidationReport, report_uri))
        
        # Add temporal metadata
        self.graph.add((report_uri, prov.startedAtTime, 
                       Literal(execution_start.isoformat(), datatype=XSD.dateTime)))
        self.graph.add((report_uri, prov.endedAtTime, 
                       Literal(execution_end.isoformat(), datatype=XSD.dateTime)))
        self.graph.add((report_uri, tb.executionDuration, 
                       Literal(execution_duration, datatype=XSD.float)))
        
        # Add result metadata
        validation_status = self._determine_validation_status(result)
        self.graph.add((report_uri, tb.validationStatus, Literal(validation_status)))
        
        # Add result type and summary
        result_type = type(result).__name__
        self.graph.add((report_uri, tb.resultType, Literal(result_type)))
        
        if isinstance(result, pd.DataFrame):
            self.graph.add((report_uri, tb.resultRowCount, 
                           Literal(len(result), datatype=XSD.integer)))
            self.graph.add((report_uri, tb.resultColumnCount, 
                           Literal(len(result.columns), datatype=XSD.integer)))
            # Add column names
            for col in result.columns:
                self.graph.add((report_uri, tb.resultColumn, Literal(str(col))))
        elif isinstance(result, (int, float)):
            self.graph.add((report_uri, tb.resultValue, 
                           Literal(result, datatype=XSD.float)))
        elif isinstance(result, bool):
            self.graph.add((report_uri, tb.resultValue, 
                           Literal(result, datatype=XSD.boolean)))
        elif isinstance(result, str):
            self.graph.add((report_uri, tb.resultValue, Literal(result)))
        
        # Add execution mode
        exec_mode = "COMPOSED_UDF" if self.compose_udf else "SEQUENTIAL"
        self.graph.add((report_uri, tb.executionMode, Literal(exec_mode)))
        
        return report_uri
    
    def _determine_validation_status(self, result: Any) -> str:
        """Determine validation status from result
        
        Args:
            result: Execution result
            
        Returns:
            Status string: PASSED, FAILED, or COMPLETED
        """
        if isinstance(result, bool):
            return "PASSED" if result else "FAILED"
        elif isinstance(result, pd.DataFrame):
            # If DataFrame is non-empty, consider it passed
            return "PASSED" if len(result) > 0 else "FAILED"
        elif isinstance(result, (int, float)):
            # Numeric results are considered passed if > 0
            return "PASSED" if result > 0 else "FAILED"
        else:
            # For other types, just mark as completed
            return "COMPLETED"
    
    def save_annotated_graph(self, output_file: str, format: str = "turtle") -> None:
        """Save the annotated graph to a file
        
        Args:
            output_file: Output file path
            format: RDF serialization format (default: turtle)
        """
        self.graph.serialize(destination=output_file, format=format)
        print(f"✓ Annotated graph saved to: {output_file}")
    
    def merge_reports_to_sdm(self, sdm_file: str, output_sdm_file: str = None) -> None:
        """Merge validation reports from PolicyChecker graph back to SDM
        
        This preserves the full SDM content while adding validation reports
        and linking them to PolicyCheckers.
        
        Args:
            sdm_file: Path to original SDM file
            output_sdm_file: Path to save merged SDM (defaults to sdm_file if None)
        """
        if output_sdm_file is None:
            output_sdm_file = sdm_file
        
        print(f"\n{'='*60}")
        print("MERGING VALIDATION REPORTS TO SDM")
        print(f"{'='*60}")
        
        # Load SDM
        sdm_graph = Graph()
        sdm_graph.parse(sdm_file, format="turtle")
        print(f"✓ Loaded SDM: {len(sdm_graph)} triples")
        
        # Bind namespaces from executor graph
        for prefix, namespace in self.graph.namespaces():
            sdm_graph.bind(prefix, namespace)
        
        # Find all validation reports in executor graph
        reports = list(self.graph.subjects(RDF.type, tb.ValidationReport))
        print(f"✓ Found {len(reports)} validation report(s) to merge")
        
        # Copy validation report triples to SDM
        report_triples = 0
        for report_uri in reports:
            # Copy all triples about this report
            for p, o in self.graph.predicate_objects(report_uri):
                sdm_graph.add((report_uri, p, o))
                report_triples += 1
            
            # Also copy the link from PolicyChecker to report
            pc_uri = self.graph.value(report_uri, prov.wasGeneratedBy)
            if pc_uri:
                # Check if this PolicyChecker exists in SDM
                if (pc_uri, RDF.type, tb.PolicyChecker) in sdm_graph:
                    sdm_graph.add((pc_uri, tb.hasValidationReport, report_uri))
                    report_triples += 1
        
        print(f"✓ Copied {report_triples} report triples to SDM")
        
        # Save merged SDM
        sdm_graph.serialize(destination=output_sdm_file, format="turtle")
        print(f"✓ Merged SDM saved to: {output_sdm_file}")
        print(f"  Total triples: {len(sdm_graph)}")
        print(f"{'='*60}\n")
        
        return sdm_graph


def load_policy_checker(pc_file: str) -> tuple[Graph, URIRef]:
    """Load PolicyChecker from file and return graph + PC URI"""
    g = Graph()
    g.parse(pc_file, format='turtle')
    
    # Find PolicyChecker URI
    pc_uri = None
    for s in g.subjects(RDF.type, tb.PolicyChecker):
        pc_uri = s
        break
    
    if not pc_uri:
        raise ValueError(f"No PolicyChecker found in {pc_file}")
    
    return g, pc_uri


def merge_validation_reports_to_sdm(pc_graph: Graph, sdm_file: str, output_sdm_file: str) -> Graph:
    """Merge validation reports from PolicyChecker graph to SDM (standalone function)
    
    Args:
        pc_graph: Graph containing PolicyCheckers with validation reports
        sdm_file: Path to original SDM file
        output_sdm_file: Path to save merged SDM
        
    Returns:
        Merged SDM graph
    """
    print(f"\n{'='*80}")
    print("MERGING VALIDATION REPORTS TO SEMANTIC DATA MODEL")
    print(f"{'='*80}")
    
    # Load SDM
    sdm_graph = Graph()
    sdm_graph.parse(sdm_file, format="turtle")
    original_count = len(sdm_graph)
    print(f"✓ Loaded SDM from: {sdm_file}")
    print(f"  Original triples: {original_count}")
    
    # Bind namespaces
    for prefix, namespace in pc_graph.namespaces():
        sdm_graph.bind(prefix, namespace)
    
    # Find all validation reports
    reports = list(pc_graph.subjects(RDF.type, tb.ValidationReport))
    print(f"\n✓ Found {len(reports)} validation report(s) in PolicyChecker graph")
    
    # Copy validation report triples
    report_triples = 0
    for report_uri in reports:
        print(f"\n  Copying report: {report_uri}")
        
        # Copy all triples about this report
        for p, o in pc_graph.predicate_objects(report_uri):
            sdm_graph.add((report_uri, p, o))
            report_triples += 1
        
        # Get associated PolicyChecker from execution graph
        exec_pc_uri = pc_graph.value(report_uri, prov.wasGeneratedBy)
        if exec_pc_uri:
            # Get policy and data product from execution graph
            policy = pc_graph.value(exec_pc_uri, tb.accordingTo)
            data_product = pc_graph.value(exec_pc_uri, tb.validates)
            
            # Find matching PolicyChecker in SDM by policy and data product
            sdm_pc_uri = None
            for pc in sdm_graph.subjects(RDF.type, tb.PolicyChecker):
                pc_policy = sdm_graph.value(pc, tb.accordingTo)
                pc_dp = sdm_graph.value(pc, tb.validates)
                
                if pc_policy == policy and pc_dp == data_product:
                    sdm_pc_uri = pc
                    break
            
            if sdm_pc_uri:
                # Link report to SDM PolicyChecker
                sdm_graph.add((sdm_pc_uri, tb.hasValidationReport, report_uri))
                # Update the report to point to SDM PolicyChecker
                sdm_graph.remove((report_uri, prov.wasGeneratedBy, exec_pc_uri))
                sdm_graph.add((report_uri, prov.wasGeneratedBy, sdm_pc_uri))
                report_triples += 2  # Link and updated wasGeneratedBy
                
                status = pc_graph.value(report_uri, tb.validationStatus)
                duration = pc_graph.value(report_uri, tb.executionDuration)
                
                print(f"    Matched to SDM PolicyChecker: {sdm_pc_uri}")
                print(f"    Policy: {policy}")
                print(f"    Data Product: {data_product}")
                print(f"    Status: {status}")
                print(f"    Duration: {float(duration):.6f}s")
            else:
                print(f"    ⚠ Warning: No matching PolicyChecker found in SDM")
                print(f"      Looking for policy={policy}, data_product={data_product}")
    
    new_count = len(sdm_graph)
    print(f"\n{'─'*80}")
    print(f"✓ Merged {report_triples} validation report triples")
    print(f"  SDM triples before: {original_count}")
    print(f"  SDM triples after: {new_count}")
    print(f"  Triples added: {new_count - original_count}")
    
    # Save merged SDM
    sdm_graph.serialize(destination=output_sdm_file, format="turtle")
    print(f"\n✓ Merged SDM saved to: {output_sdm_file}")
    print(f"{'='*80}\n")
    
    return sdm_graph


if __name__ == "__main__":
    import sys
    import os
    
    if len(sys.argv) != 3:
        print("Usage: python executor_semantic.py <policy_checker_uri> <code_metadata.json>")
        sys.exit(1)
    
    pc_uri_str = sys.argv[1]
    metadata_file = sys.argv[2]
    
    # SINGLE SOURCE OF TRUTH: sdm.ttl in SemanticDataModel folder
    # All components (federation, registration, contracts, planner, executor) use this file
    script_dir = Path(__file__).parent
    sdm_base_dir = script_dir.parent.parent.parent / 'FederatedComputationalGovernance' / 'SemanticDataModel'
    sdm_file = str(sdm_base_dir / 'sdm.ttl')
    
    if not Path(sdm_file).exists():
        raise FileNotFoundError(f"SDM not found: {sdm_file}")
    
    print(f"✓ Using SDM (single source of truth): {sdm_file}")
    
    # Output will overwrite the main SDM file with validation reports
    output_sdm = sdm_file
    
    # Load PolicyChecker from SDM
    pc_graph = Graph()
    pc_graph.parse(sdm_file, format='turtle')
    
    # Convert string to URIRef
    pc_uri = URIRef(pc_uri_str)
    
    # Verify PolicyChecker exists
    if (pc_uri, RDF.type, tb.PolicyChecker) not in pc_graph:
        raise ValueError(f"PolicyChecker not found in SDM: {pc_uri_str}")
    
    pc_uris = [pc_uri]
    
    # Get metadata for display
    policy = pc_graph.value(pc_uri, tb.accordingTo)
    dp = pc_graph.value(pc_uri, tb.validates)
    policy_name = str(policy).split('#')[-1] if policy else "Unknown"
    dp_name = str(dp).split('#')[-1] if dp else "Unknown"
    
    print(f"\n✓ Executing PolicyChecker:")
    print(f"  URI: {pc_uri_str}")
    print(f"  Policy: {policy_name}")
    print(f"  Data Product: {dp_name}")
    
    # Create executor
    executor = SemanticN3Executor(pc_graph, metadata_file)
    
    # Execute ALL PolicyCheckers
    results = []
    for i, pc_uri in enumerate(pc_uris, 1):
        print(f"\n{'='*80}")
        print(f"EXECUTING POLICYCHECKER {i}/{len(pc_uris)}")
        print(f"{'='*80}")
        
        result = executor.execute(pc_uri)
        results.append(result)
    
    print(f"\n{'='*80}")
    print(f"✅ Executed {len(pc_uris)} PolicyChecker(s)")
    print(f"{'='*80}")
    
    # Merge validation reports back to main SDM
    print(f"\n{'='*80}")
    print("MERGING VALIDATION REPORTS TO MAIN SDM")
    print(f"{'='*80}\n")
    
    merged_sdm = merge_validation_reports_to_sdm(
        pc_graph=executor.graph,
        sdm_file=sdm_file,
        output_sdm_file=output_sdm
    )
    
    print(f"✅ Validation reports successfully merged to: {output_sdm}")
