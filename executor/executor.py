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
import re
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
        
    def get_gx_expectation_type(self, impl_uri: URIRef) -> str:
        """Get Great Expectations type for implementation"""
        impl_id = str(impl_uri)
        impl = self.implementations.get(impl_id)
        
        if not impl:
            return None
            
        return impl.get('tb:gxExpectationType')
    
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
    
    def __init__(self, pc_graph: Graph, code_metadata_file: str, compose_udf: bool = True, annotate_graph: bool = True, export_docker_dir: str = None, backend: str = "pandas"):
        self.graph = pc_graph
        self.code_metadata = CodeMetadataLoader(code_metadata_file)
        self.operations_cache = {}
        self.compose_udf = compose_udf  # Flag to use composed UDF approach (default: True)
        self.annotate_graph = annotate_graph  # Flag to annotate graph with execution metadata
        self.export_docker_dir = export_docker_dir  # Directory to output Docker services
        self.backend = backend # pandas | gx
        
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
                # Patch EYE reasoned output where non-floats get tagged as floats (e.g. "true"^^xsd:float)
                # rdflib strict parser fails on this, so we strip the explicit datatype cast.
                safe_stdout = re.sub(r'(?i)"(true|false)"\^\^(?:<http://www.w3.org/2001/XMLSchema#float>|xsd:float|xsd:double|<http://www.w3.org/2001/XMLSchema#double>)', r'"\1"', result.stdout)
                
                
                result_graph.parse(data=safe_stdout, format='n3')
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
        result, composed_code = self._execute_chain_composed(first_op, pc_uri=pc_uri)
        
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
    
    def _execute_chain_composed(self, op_uri: URIRef, previous_result: Any = None, pc_uri: URIRef = None) -> Any:
        """Execute operation chain - Option 2: Compose all UDFs then execute"""
        
        if self.backend == 'gx':
            return self._execute_chain_gx(op_uri, previous_result, pc_uri)
            
        # Step 1: Collect all operations and compose UDF
        print(f"\n{'='*60}")
        print("COMPOSING PANDAS UDF PIPELINE")
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
                # Subsequent operations consume the previous result. Substitute
                # only the standalone `data` identifier - a bare str.replace()
                # also rewrites substrings, so a template mentioning
                # `data_element` or `metadata` would be silently corrupted.
                code = re.sub(r'\bdata\b', f'result_{i-1}', op['code_template'])
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    result_{i} = {code}\n\n"
        
        composed_code += f"    return result_{len(operations)-1}\n"
        
        print(composed_code)
        print(f"{'='*60}\n")
        
        # Export docker service if configured
        if getattr(self, 'export_docker_dir', None) and pc_uri:
            print("  ⚠ Semantic Docker export is currently unsupported. Skipping standalone generation.")
            # self._export_docker_service(pc_uri, composed_code, operations, previous_result)
        
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
    
    def _execute_chain_gx(self, op_uri: URIRef, previous_result: Any = None, pc_uri: URIRef = None) -> Any:
        print(f"\n{'='*60}")
        print("COMPOSING GX EXPECTATION SUITE")
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
            
            bindings = self.get_semantic_bindings(current_op, None)
            gx_expectation_type = self.code_metadata.get_gx_expectation_type(impl_uri)
            
            operations.append({
                'num': op_count,
                'uri': current_op,
                'type': op_type,
                'impl': impl_uri,
                'bindings': bindings,
                'gx_type': gx_expectation_type
            })
            
            print(f"  Step {op_count}: {op_type}")
            print(f"    GX Type: {gx_expectation_type}")
            
            # Check if terminal
            is_terminal = self.graph.value(current_op, tb.isTerminal)
            if is_terminal and is_terminal.toPython():
                break
            
            current_op = self.graph.value(current_op, tb.nextStep)
            
        print(f"\n{'='*60}")
        print("COMPOSED GX SCRIPT:")
        print(f"{'='*60}")
        
        composed_code = "import great_expectations as gx\n"
        composed_code += "import pandas as pd\n\n"
        composed_code += "def run_gx_validation(data_path):\n"
        composed_code += "    context = gx.get_context(mode='ephemeral')\n"
        composed_code += "    _df = pd.read_csv(data_path)\n"
        composed_code += "    suite = context.suites.add(gx.ExpectationSuite(name='semantic_suite'))\n\n"
        
        needs_dataframe_source = False
        freshness_ts_col = None
        freshness_age_col = None
        
        for i, op in enumerate(operations):
            if not op['gx_type'] or op['gx_type'] == 'load_data':
                continue
                
            if op['gx_type'].startswith('constraint_'):
                continue
                
            b = op['bindings']
            kwargs = {}
            if 'attr' in b:
                kwargs['column'] = b['attr']
            elif 'ts_attr' in b:
                kwargs['column'] = b['ts_attr']
                
            threshold = None
            operator = None
            if i + 1 < len(operations) and operations[i+1]['gx_type'].startswith('constraint_'):
                next_op = operations[i+1]
                next_b = next_op['bindings']
                if 'threshold' in next_b:
                    threshold = next_b['threshold']
                if 'operator' in next_b:
                    operator = str(next_b['operator'])

            if threshold is not None:
                if operator == 'http://www.w3.org/ns/odrl/2/isIncludedIn':
                    # isIncludedIn: strict set membership (no mostly threshold)
                    pass
                elif op['gx_type'] == 'expect_column_kl_divergence_to_be_less_than':
                    kwargs['threshold'] = threshold / 100.0
                    # partition_object built dynamically at runtime from data
                elif op['gx_type'] == 'expect_column_values_to_be_between':
                    kwargs['min_value'] = 0
                    kwargs['max_value'] = threshold
                elif op['gx_type'] == 'expect_table_row_count_to_be_between':
                    if operator == 'http://www.w3.org/ns/odrl/2/gteq':
                        kwargs['min_value'] = threshold
                        kwargs['max_value'] = None
                    elif operator == 'http://www.w3.org/ns/odrl/2/lteq':
                        kwargs['min_value'] = 0
                        kwargs['max_value'] = threshold
                    else:
                        kwargs['min_value'] = threshold
                        kwargs['max_value'] = threshold
                else:
                    kwargs['mostly'] = threshold / 100.0
            
            if op['gx_type'] == 'expect_column_values_to_not_be_null':
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    suite.add_expectation(gx.expectations.ExpectColumnValuesToNotBeNull(**{kwargs}))\n\n"
            
            elif op['gx_type'] == 'expect_column_values_to_be_in_set':
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    import pycountry\n"
                composed_code += f"    _kwargs = {repr(kwargs)}\n"
                composed_code += f"    _kwargs['value_set'] = [c.alpha_2 for c in pycountry.countries]\n"
                composed_code += f"    suite.add_expectation(gx.expectations.ExpectColumnValuesToBeInSet(**_kwargs))\n\n"
                
            elif op['gx_type'] == 'expect_column_kl_divergence_to_be_less_than':
                # Fairness: build uniform partition dynamically from data categories
                fairness_col = kwargs.get('column', 'gender')
                needs_dataframe_source = True
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    _cats = sorted(_df['{fairness_col}'].dropna().unique().tolist())\n"
                composed_code += f"    _uniform = {{\"values\": _cats, \"weights\": [1.0/len(_cats)]*len(_cats)}}\n"
                composed_code += f"    _kl_kwargs = {repr(kwargs)}\n"
                composed_code += f"    _kl_kwargs['partition_object'] = _uniform\n"
                composed_code += f"    suite.add_expectation(gx.expectations.ExpectColumnKLDivergenceToBeLessThan(**_kl_kwargs))\n\n"
                
            elif op['gx_type'] == 'expect_column_values_to_be_between':
                # Freshness check: need to compute age in minutes from datetime column
                ts_col = kwargs.get('column', 'lastUpdated')
                age_col = f"{ts_col}_age_minutes"
                needs_dataframe_source = True
                freshness_ts_col = ts_col
                freshness_age_col = age_col
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    # Pre-compute data age in minutes from datetime column\n"
                age_kwargs = dict(kwargs)
                age_kwargs['column'] = age_col
                composed_code += f"    suite.add_expectation(gx.expectations.ExpectColumnValuesToBeBetween(**{age_kwargs}))\n\n"
                
            elif op['gx_type'] == 'expect_table_row_count_to_be_between':
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    suite.add_expectation(gx.expectations.ExpectTableRowCountToBeBetween(**{kwargs}))\n\n"
                
            elif op['gx_type'] == 'expect_column_values_to_be_null':
                # Build row_condition dynamically from refinement parameters
                condition_attr = b.get('condition_attr')
                condition_values = b.get('condition_values', '')
                if condition_attr and condition_values:
                    parts = [f'{condition_attr} == "{v.strip()}"' for v in condition_values.split(',')]
                    kwargs['row_condition'] = ' or '.join(parts)
                    kwargs['condition_parser'] = 'pandas'
                if 'mostly' in kwargs:
                    del kwargs['mostly']
                composed_code += f"    # Step {op['num']}: {op['type']}\n"
                composed_code += f"    suite.add_expectation(gx.expectations.ExpectColumnValuesToBeNull(**{kwargs}))\n\n"

                
        if needs_dataframe_source:
            # DataFrame-based asset (needed for dynamic partition or computed columns)
            if freshness_age_col and freshness_ts_col:
                composed_code += f"    _df['{freshness_age_col}'] = (pd.Timestamp.now() - pd.to_datetime(_df['{freshness_ts_col}'])).dt.total_seconds() / 60.0\n"
            composed_code += "    ds = context.data_sources.add_pandas('pandas_source')\n"
            composed_code += "    da = ds.add_dataframe_asset(name='df_asset')\n"
            composed_code += "    batch_def = da.add_batch_definition_whole_dataframe('dataframe_batch_def')\n"
            composed_code += "    validation_definition = gx.ValidationDefinition(name='semantic_validation', data=batch_def, suite=suite)\n"
            composed_code += "    context.validation_definitions.add(validation_definition)\n"
            composed_code += "    checkpoint = gx.Checkpoint(name='semantic_checkpoint', validation_definitions=[validation_definition])\n"
            composed_code += "    context.checkpoints.add(checkpoint)\n"
            composed_code += "    result = checkpoint.run(batch_parameters={'dataframe': _df})\n"
        else:
            composed_code += "    csv_asset = context.data_sources.add_pandas('pandas_source').add_csv_asset('csv_asset', filepath_or_buffer=data_path)\n"
            composed_code += "    batch_def = csv_asset.add_batch_definition_whole_dataframe('dataframe_batch_def')\n"
            composed_code += "    validation_definition = gx.ValidationDefinition(name='semantic_validation', data=batch_def, suite=suite)\n"
            composed_code += "    context.validation_definitions.add(validation_definition)\n"
            composed_code += "    checkpoint = gx.Checkpoint(name='semantic_checkpoint', validation_definitions=[validation_definition])\n"
            composed_code += "    context.checkpoints.add(checkpoint)\n"
            composed_code += "    result = checkpoint.run()\n"
        composed_code += "    return result.success\n"
        
        print(composed_code)
        print(f"{'='*60}\n")
        
        if getattr(self, 'export_docker_dir', None) and pc_uri:
            self._export_docker_service_gx(pc_uri, composed_code, operations, previous_result)
        else:
            # Even without --export-docker, we need a temp dir to build+run
            tmp_export = Path(tempfile.mkdtemp(prefix="pc_gx_"))
            self._export_docker_service_gx(pc_uri, composed_code, operations, previous_result,
                                           override_dir=tmp_export)

        # ── Build & Run the PolicyChecker container ──
        hash_id = hashlib.md5(str(pc_uri).encode()).hexdigest()[:8]
        if getattr(self, 'export_docker_dir', None):
            service_dir = Path(self.export_docker_dir) / f"policyChecker_{hash_id}"
        else:
            service_dir = tmp_export / f"policyChecker_{hash_id}"

        image_tag = f"policychecker-{hash_id}:latest"

        # Resolve data path — handle host vs container path differences
        data_path = None
        first_bindings = self.get_semantic_bindings(operations[0]['uri'], previous_result)
        if 'p' in first_bindings:
            data_path = first_bindings['p']

        data_file = Path(data_path) if data_path else None
        if not data_file or not data_file.exists():
            # Try to locate the file under common base directories
            candidates = []
            if data_path:
                # Extract the relative part after a known project marker
                for marker in ("DataProductLayer/", "DataProduct_EHDS_AMR/"):
                    idx = data_path.find(marker)
                    if idx != -1:
                        rel = data_path[idx:]
                        candidates += [Path("/app") / rel, Path("/project") / rel]
                        break
            candidates.append(Path("/project/DataProductLayer/DataProduct_EHDS_AMR/Data/Patient_Summary.csv"))
            candidates.append(Path("/app/DataProductLayer/DataProduct_EHDS_AMR/Data/Patient_Summary.csv"))
            for c in candidates:
                if c.exists():
                    data_file = c
                    break

        if not data_file or not data_file.exists():
            print(f"  ✗ Data file not found: {data_path}")
            return False, composed_code

        print(f"\n  Building PolicyChecker image: {image_tag}")
        build_result = subprocess.run(
            ["docker", "build", "-t", image_tag, "."],
            cwd=str(service_dir), capture_output=True, text=True
        )
        if build_result.returncode != 0:
            print(f"  ✗ Docker build failed:\n{build_result.stderr}")
            return False, composed_code

        print(f"  ✓ Image built: {image_tag}")

        # Run the container — mount data directory and pass file path
        print(f"  Running validation container...")
        data_dir = str(data_file.parent)
        data_name = data_file.name
        run_cmd = [
            "docker", "run", "--rm",
            "-v", f"{data_dir}:/data",
            image_tag,
            f"/data/{data_name}"
        ]
        run_result = subprocess.run(run_cmd, capture_output=True, text=True, timeout=300)

        print(run_result.stdout)
        if run_result.stderr:
            print(f"  stderr: {run_result.stderr}")

        # Parse result from stdout
        result = False
        for line in run_result.stdout.strip().splitlines():
            stripped = line.strip()
            if stripped in ("True", "true"):
                result = True
                break
            elif stripped in ("False", "false"):
                result = False
                break

        print(f"  ✓ PolicyChecker container result: {result}\n")

        # Clean up temp dir if we created one
        if 'tmp_export' in locals():
            shutil.rmtree(str(tmp_export), ignore_errors=True)

        return result, composed_code
        
    def _export_docker_service_gx(self, pc_uri: URIRef, composed_code: str, operations: list, previous_result: Any, override_dir: Path = None):
        """Export the GX composed suite as a standalone Dockerized service"""
        print(f"\n{'='*60}")
        print("EXPORTING GX DOCKERIZED VALIDATION SERVICE")
        print(f"{'='*60}")
        
        hash_id = hashlib.md5(str(pc_uri).encode()).hexdigest()[:8]
        base_dir = override_dir if override_dir else Path(self.export_docker_dir)
        service_dir = base_dir / f"policyChecker_{hash_id}"
        service_dir.mkdir(parents=True, exist_ok=True)
        
        script_path = service_dir / "validation_job.py"
        # Every composed template loads through pandas, so the job needs it
        # imported. Excluding it here produced a container that built cleanly and
        # then died at run time with NameError: name 'pandas' is not defined.
        imports = {'great_expectations', 'pandas'}
        for op in operations:
            for dep in self.code_metadata.get_dependencies(op['impl']):
                imports.add(dep)
                    
        with open(script_path, "w") as f:
            for imp in imports:
                f.write(f'import {imp}\n')
            f.write('\n')
            f.write(composed_code)
            f.write('\n\n')
            f.write('if __name__ == "__main__":\n')
            f.write('    import sys\n')
            
            f.write('    # Extract data path from args or use default\n')
            f.write('    if len(sys.argv) < 2:\n')
            f.write('        print("Usage: python validation_job.py <data_path>")\n')
            f.write('        sys.exit(1)\n')
            f.write('    data_file = sys.argv[1]\n')
            f.write('    print(f"Loading data from {data_file}...")\n')
            f.write('    try:\n')
            f.write('        result = run_gx_validation(data_file)\n')
            f.write('        print("\\n=== VALIDATION RESULT ===")\n')
            f.write('        print(result)\n')
            f.write('        print("===========================")\n')
            f.write('    except Exception as e:\n')
            f.write('        print(f"Error during validation: {e}")\n')
            f.write('        sys.exit(1)\n')
            
        print(f"  ✓ Generated validation_job.py (GX)")
        
        # One line per distinct dependency; great_expectations carries its floor,
        # everything else is whatever the code metadata declared.
        pinned = {'great_expectations': 'great_expectations>=1.0.0'}
        reqs_path = service_dir / "requirements.txt"
        with open(reqs_path, "w") as f:
            for imp in sorted(imports - {'datetime'}):   # datetime is stdlib
                f.write(pinned.get(imp, imp) + '\n')
        print(f"  ✓ Generated requirements.txt (GX)")
        
        docker_path = service_dir / "Dockerfile"
        with open(docker_path, "w") as f:
            f.write('FROM python:3.9-slim\n\n')
            f.write('WORKDIR /app\n\n')
            f.write('COPY requirements.txt .\n')
            f.write('RUN pip install --no-cache-dir -r requirements.txt\n\n')
            f.write('COPY validation_job.py .\n\n')
            f.write('ENTRYPOINT ["python", "validation_job.py"]\n')
            
        print(f"  ✓ Generated Dockerfile (GX)")
        print(f"  Service ready at {service_dir}")
        print(f"{'='*60}\n")

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
                val_str = str(value_node)
                # Parse manual
                if val_str.lower() == 'true':
                    bindings[name] = True
                elif val_str.lower() == 'false':
                    bindings[name] = False
                else:
                    try:
                        bindings[name] = int(val_str)
                    except ValueError:
                        try:
                            bindings[name] = float(val_str)
                        except ValueError:
                            bindings[name] = val_str
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
            str(abox.CheckCompleteness): str(abox.ImpQualityMetric),
            str(abox.CheckValidity): str(abox.ImpQualityMetricValidity),
            str(abox.CheckFairness): str(abox.ImpQualityMetricFairness),
            str(abox.CheckVolume): str(abox.ImpQualityMetricRowCount),
            str(abox.CheckConsistency): str(abox.ImpQualityMetricConsistency),
            str(odrl.Constraint): str(abox.ImpConstraintLteq),
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
        elif isinstance(result, (bool, np.bool_)):
            # np.bool_ must be named explicitly and coerced: it is not a
            # subclass of bool, so verdicts produced by pandas/numpy
            # comparisons were previously recorded with no tb:resultValue.
            self.graph.add((report_uri, tb.resultValue,
                           Literal(bool(result), datatype=XSD.boolean)))
        elif isinstance(result, (int, float, np.integer, np.floating)):
            self.graph.add((report_uri, tb.resultValue,
                           Literal(float(result), datatype=XSD.float)))
        elif isinstance(result, str):
            self.graph.add((report_uri, tb.resultValue, Literal(result)))
        
        # Add execution mode
        exec_mode = "COMPOSED_UDF" if self.compose_udf else "SEQUENTIAL"
        self.graph.add((report_uri, tb.executionMode, Literal(exec_mode)))
        
        return report_uri
    
    def _determine_validation_status(self, result: Any) -> str:
        """Determine validation status from a terminal operation's result.

        Only a boolean is a verdict. The DQVP rules chain every measurement
        into a terminal odrl:Constraint operation, so a well-formed
        PolicyChecker always ends in one.

        Anything else means no constraint was evaluated - a chain that stopped
        at its measurement, leaving odrl:operator and odrl:rightOperand
        unapplied. Those report COMPLETED rather than being guessed at.
        Inferring PASSED from "the number is positive" or "the frame is
        non-empty" silently reports an unenforced requirement as satisfied:
        a completeness score of 8.94 would pass a 99% threshold.

        Args:
            result: Execution result

        Returns:
            Status string: PASSED, FAILED, or COMPLETED
        """
        # np.bool_ is not a subclass of Python bool, so it needs naming
        # explicitly. np.bool_ is the portable spelling - the np.bool alias was
        # removed in numpy 1.24 and only restored in 2.0.
        if isinstance(result, (bool, np.bool_)):
            return "PASSED" if result else "FAILED"

        if isinstance(result, pd.Series) and result.dtype == bool:
            # An all-True boolean mask is a verdict over every row.
            return "PASSED" if result.all() else "FAILED"

        print(
            f"      ⚠ Terminal operation returned {type(result).__name__}, not a "
            f"verdict - no constraint was evaluated. Reporting COMPLETED."
        )
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
    import argparse
    from pathlib import Path
    from rdflib import Graph, URIRef, RDF, Namespace
    tb = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
    from rdflib.namespace import PROV as prov
    
    parser = argparse.ArgumentParser(description="Semantic N3-Based Executor")
    parser.add_argument("pc_uri", help="PolicyChecker URI")
    parser.add_argument("metadata_file", help="Path to code_metadata.json")
    parser.add_argument("--export-docker", type=str, help='Export standalone Docker validation services to DIR', default=None)
    parser.add_argument('--backend', type=str, choices=['pandas', 'gx'], default='pandas',
                        help='Execution backend to use (pandas or gx)')
    args = parser.parse_args()
    
    pc_uri_str = args.pc_uri
    metadata_file = args.metadata_file
    export_docker_dir = args.export_docker
    backend = args.backend
    
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
    with open(sdm_file, 'r') as f:
        sdm_content = f.read()
    
    # Patch RDFlLib parser bug with boolean float types
    sdm_content = re.sub(r'(?i)"(true|false)"\^\^(?:<http://www.w3.org/2001/XMLSchema#float>|xsd:float|xsd:double|<http://www.w3.org/2001/XMLSchema#double>)', r'"\1"', sdm_content)
    pc_graph.parse(data=sdm_content, format='turtle')
    
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
    
    # 2. Find and execute PolicyChecker
    executor = SemanticN3Executor(pc_graph, metadata_file, export_docker_dir=export_docker_dir, backend=backend)
    
    # We should search for the exact pc_uri
    target_pc = URIRef(pc_uri_str)
    
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
