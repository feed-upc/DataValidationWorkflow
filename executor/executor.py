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
import subprocess
import tempfile
import os
import shutil
import numpy as np
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
    
    def apply_n3_bindings(self, pc_uri: URIRef):
        """
        Apply N3 rules to bind parameters.
        Generates tb:boundParameter triples.
        """
        print(f"Applying N3 parameter binding rules for {pc_uri}")
        
        # Create subgraph for reasoning
        pc_subgraph = Graph()
        # Bind prefixes
        pc_subgraph.bind('tb', tb)
        pc_subgraph.bind('ab', abox)
        pc_subgraph.bind('odrl', Namespace("http://www.w3.org/ns/odrl/2/"))
        pc_subgraph.bind('dqv', Namespace("http://www.w3.org/ns/dqv#"))
        
        # Add relevant triples
        self._add_operation_chain(self.graph, pc_subgraph, pc_uri)
        
        # Create local temp dir to avoid absolute path issues with EYE (Node wrapper)
        temp_dir = Path.cwd() / f"eye_temp_{int(time.time()*1000)}"
        temp_dir.mkdir(exist_ok=True)
        
        try:
            # Save data
            data_file = temp_dir / "data.n3"
            pc_subgraph.serialize(destination=str(data_file), format='n3')
            
            # Copy rules
            original_rules = Path(__file__).parent / "parameter_binding_semantic.n3"
            if not original_rules.exists():
                raise FileNotFoundError(f"Rules not found: {original_rules}")
                
            rules_file = temp_dir / "rules.n3"
            shutil.copy(original_rules, rules_file)
            
            # Resolve eye path
            eye_path = self._resolve_eye_path()
            
            # Run EYE with relative paths from temp_dir
            cmd = [eye_path, 'data.n3', 'rules.n3', '--pass', '--nope']
            
            # print(f"Running EYE in {temp_dir}: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd, 
                cwd=str(temp_dir),
                capture_output=True, 
                text=True
            )
            
            if result.returncode != 0:
                print(f"Error running EYE: {result.stderr}")
                raise RuntimeError(f"EYE failed: {result.stderr}")
                
            # Parse results
            result_graph = Graph()
            # Bind prefixes again for parsing safety
            result_graph.bind('tb', tb)
            try:
                result_graph.parse(data=result.stdout, format='n3')
                # print(f"✓ Generated {len(result_graph)} triples from reasoning")
            except Exception as e:
                print(f"Error parsing EYE output: {e}")
                # print(f"Raw output start: {result.stdout[:200]}")
                raise

            # Merge bound parameters back to main graph
            count = 0
            for s, p, o in result_graph:
                self.graph.add((s, p, o))
                count += 1
                
            print(f"✓ Merged {count} inferred triples (bindings)")
            
        finally:
            # Cleanup
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
                
        return self.graph

    def _add_operation_chain(self, source_graph: Graph, target_graph: Graph, pc_uri: URIRef):
        """Add all operations in chain to target graph (helper for N3 binding)"""
        # Start from first operation
        first_op = source_graph.value(pc_uri, tb.nextStep)
        if not first_op:
            return
        
        current = first_op
        visited = set()
        
        while current and current not in visited:
            visited.add(current)
            
            # Add all triples about this operation
            for p, o in source_graph.predicate_objects(current):
                target_graph.add((current, p, o))
                # Include hasInput details
                if isinstance(o, (URIRef, BNode)) and p == tb.hasInput:
                    for ip, io in source_graph.predicate_objects(o):
                        target_graph.add((o, ip, io))
            
            # Move to next operation
            current = source_graph.value(current, tb.nextStep)
            
    def _resolve_eye_path(self) -> str:
        """Resolve path to eye reasoner"""
        # 1. Check PATH
        if shutil.which("eye"):
            return "eye"
            
        # 2. Check node_modules in likely locations
        # Current dir
        cwd_node = Path.cwd() / "node_modules" / ".bin" / "eye"
        if cwd_node.exists():
            return str(cwd_node)
            
        # Project root (up 3 levels from executor)
        project_node = Path(__file__).parent.parent.parent.parent / "node_modules" / ".bin" / "eye"
        if project_node.exists():
            return str(project_node)
            
        raise FileNotFoundError("Could not find 'eye' reasoner. Please install it via npm or add to PATH.")
    
    def execute(self, pc_uri: URIRef) -> Tuple[Any, Optional[URIRef]]:
        """Execute a PolicyChecker and optionally annotate with execution metadata
        
        Returns:
            Tuple of (result, validation_report_uri)
        """
        print(f"\n{'='*60}")
        print(f"Executing PolicyChecker: {pc_uri}")
        print(f"Execution Mode: COMPOSED UDF")
        print(f"{'='*60}\n")
        
        # Step 0: Apply N3 Parameter Bindings
        self.apply_n3_bindings(pc_uri)
        
        # Find first operation
        first_op = self.graph.value(pc_uri, tb.nextStep)
        if not first_op:
            raise ValueError(f"No operations found for PolicyChecker {pc_uri}")
        
        # Track execution time
        start_time = time.time()
        execution_start = datetime.now()
        
        # Execute operation chain using composed UDF approach
        result, composed_code = self._execute_chain_composed(first_op)
        
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
                execution_duration=execution_duration,
                composed_code=composed_code
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
            # Ensure implementation is linked in the graph
            if not self.graph.value(op['uri'], tb.hasImplementation):
                self.graph.add((op['uri'], tb.hasImplementation, op['impl']))
                print(f"    + Linked implementation {op['impl']} to {op['uri']}")

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
        
        return result, composed_code
    
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
        Extract parameter bindings from N3-INFERRED tb:boundParameter nodes.
        
        The N3 rules have already transformed:
            hasInput(role="filePath", value=X) -> boundParameter(name="p", value=X)
        """
        bindings = {}
        
        # Iterate through all boundParameter nodes created by/inferred for this operation
        for bound_param in self.graph.objects(op_uri, tb.boundParameter):
            name_node = self.graph.value(bound_param, tb.name)
            value_node = self.graph.value(bound_param, tb.value)
            
            if not name_node or not value_node:
                continue
                
            name = str(name_node)
            
            # Handle special dynamic parameters
            if name == "data":
                 # 'data' parameter usually expects the dataframe from the pipeline
                 # If previous_result is available, use it.
                 # The N3 rule might bind it to a placeholder URI or previous op URI
                 if previous_result is not None:
                     bindings['data'] = previous_result
                 continue # Skip adding the graph value for 'data'

            # Convert value based on type/content
            if isinstance(value_node, Literal):
                # Literals (int, float, bool)
                bindings[name] = value_node.toPython()
            else:
                # URIs or strings
                bindings[name] = str(value_node)

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
                                   execution_duration: float, composed_code: str = None) -> URIRef:
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
        
        # Add generated code if available
        if composed_code:
            self.graph.add((report_uri, tb.hasComposedCode, Literal(composed_code)))
        
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
        elif isinstance(result, pd.Series):
             self.graph.add((report_uri, tb.resultRowCount,
                           Literal(len(result), datatype=XSD.integer)))
             self.graph.add((report_uri, tb.resultValue,
                           Literal(str(result.head().to_dict()))))
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
        elif isinstance(result, pd.Series) and result.dtype == bool:
            # Boolean series must be all True
            return "PASSED" if result.all() else "FAILED"
        elif isinstance(result, (np.bool_, np.bool)):
            return "PASSED" if result else "FAILED"
        elif isinstance(result, (np.integer, np.floating)):
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


def _copy_subgraph(source_graph: Graph, target_graph: Graph, node: Any, visited: set = None) -> None:
    """Recursively copy a subgraph (CBD-like) for a node, following BNodes"""
    if visited is None:
        visited = set()
        
    if node in visited:
        return
    visited.add(node)
    
    # Copy all outgoing triples
    for p, o in source_graph.predicate_objects(node):
        target_graph.add((node, p, o))
        # Recurse on BNodes
        if isinstance(o, BNode):
            _copy_subgraph(source_graph, target_graph, o, visited)


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


def merge_validation_reports_to_sdm(pc_graph: Graph, sdm_file: str, output_sdm_file: str, metadata_file: str = None) -> Graph:
    """Merge validation reports from PolicyChecker graph to SDM (standalone function)
    
    Args:
        pc_graph: Graph containing PolicyCheckers with validation reports
        sdm_file: Path to original SDM file
        output_sdm_file: Path to save merged SDM
        metadata_file: Path to code metadata file (JSON-LD) to merge implementation details
        
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
    
    # Copy implementation links and details
    impl_triples = 0
    meta_graph = None
    
    # Load metadata graph if file provided
    if metadata_file and os.path.exists(metadata_file):
        try:
            print(f"  Loading metadata from: {metadata_file}")
            meta_graph = Graph()
            meta_graph.parse(metadata_file, format='json-ld')
            print(f"  ✓ Loaded {len(meta_graph)} metadata triples")
        except Exception as e:
            print(f"  ⚠ Failed to load metadata (dependency issue?): {e}")

    # Find used implementations
    used_impls = set(pc_graph.objects(None, tb.hasImplementation))
    print(f"  Found {len(used_impls)} used implementation(s)")

    for impl_uri in used_impls:
        # 1. Link in SDM (Executor -> SDM)
        # Find who uses it in pc_graph
        for s in pc_graph.subjects(tb.hasImplementation, impl_uri):
            sdm_graph.add((s, tb.hasImplementation, impl_uri))
            impl_triples += 1
            
        # 2. Copy metadata (Metadata -> SDM)
        if meta_graph:
            # Copy all details for this implementation
            _copy_subgraph(meta_graph, sdm_graph, impl_uri)
            # Count added triples (approximation)
            added = len(list(meta_graph.predicate_objects(impl_uri)))
            impl_triples += added

    print(f"✓ Copied/Merged ~{impl_triples} implementation-related triples")
    
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
    try:
        for i, pc_uri in enumerate(pc_uris, 1):
            print(f"\n{'='*80}")
            print(f"EXECUTING POLICYCHECKER {i}/{len(pc_uris)}")
            print(f"{'='*80}")
            
            result = executor.execute(pc_uri)
            results.append(result)
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stdout)
        sys.exit(1)
    
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
        output_sdm_file=output_sdm,
        metadata_file=metadata_file
    )
    
    print(f"✅ Validation reports successfully merged to: {output_sdm}")
