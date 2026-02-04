"""
N3 Logic-Based Policy Planner for Federated Data Validation Framework (SEMANTIC VERSION)
========================================================================

This module implements the PLANNER phase using N3 logic rules with SEMANTIC properties.
Instead of flat hasInput, it uses role-specific properties for unambiguous parameter binding.

Key Changes from Original:
    - Uses semantic properties (tb:hasFilePath, tb:hasTimestampAttribute, etc.)
    - Structured odrl:constraint blocks instead of flat hasInput triples
    - Unambiguous parameter binding (no type-based guessing needed)
    - Scalable to multi-parameter operations

Author: acraf
Version: 4.0.0-semantic
Last Updated: December 16, 2025
"""

import subprocess
import tempfile
import hashlib
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
import logging

from rdflib import Graph, Namespace, URIRef, Literal, RDF, RDFS, BNode

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# NAMESPACE DEFINITIONS
# ============================================================================

# Core namespaces
tbox = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
abox = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#")
tb = tbox  # Alias for compatibility

# Standard namespaces
odrl = Namespace("http://www.w3.org/ns/odrl/2/")
dqv = Namespace("http://www.w3.org/ns/dqv#")
dcat = Namespace("http://www.w3.org/ns/dcat#")

# N3 reasoning namespaces
log = Namespace("http://www.w3.org/2000/10/swap/log#")
math = Namespace("http://www.w3.org/2000/10/swap/math#")
string = Namespace("http://www.w3.org/2000/10/swap/string#")


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class N3PlannerConfig:
    """Configuration for semantic N3-based planner"""
    n3_reasoner: str = "eye"
    enable_proof_generation: bool = False
    reasoner_timeout: int = 30
    rules_directory: str = "rules_n3_semantic"  # SEMANTIC rule directory
    eye_path: str = "eye"
    timeout: int = 30
    n3_query_all: str = """
@prefix : <http://www.w3.org/2000/10/swap/log#> .
{ ?s ?p ?o } => { ?s ?p ?o } .
"""


# ============================================================================
# BLANK NODE SKOLEMIZER (Custom Hash-Based)
# ============================================================================

def skolemize_blank_nodes(graph: Graph) -> Graph:
    """
    Replace blank nodes with deterministic URIs based on content hash.
    Format: abox:genid-<hash>
    """
    skolemized = Graph()
    
    # Copy namespace bindings
    for prefix, namespace in graph.namespaces():
        skolemized.bind(prefix, namespace)
    
    # Map blank nodes to skolem URIs
    bnode_map = {}
    
    for s, p, o in graph:
        # Skolemize subject if blank
        if isinstance(s, BNode):
            if s not in bnode_map:
                content = f"{s}_{len(bnode_map)}"
                hash_val = hashlib.md5(content.encode()).hexdigest()[:12]
                bnode_map[s] = URIRef(f"{abox}genid-{hash_val}")
            s = bnode_map[s]
        
        # Skolemize object if blank
        if isinstance(o, BNode):
            if o not in bnode_map:
                content = f"{o}_{len(bnode_map)}"
                hash_val = hashlib.md5(content.encode()).hexdigest()[:12]
                bnode_map[o] = URIRef(f"{abox}genid-{hash_val}")
            o = bnode_map[o]
        
        skolemized.add((s, p, o))
    
    return skolemized


# ============================================================================
# N3 REASONER WRAPPER
# ============================================================================

class EYEReasoner:
    """Wrapper for EYE N3 reasoner with semantic rule support"""
    
    def __init__(self, config: N3PlannerConfig):
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.EYEReasoner")
    
    def apply_rules(self, data_graph: Graph, rules_dir: Path) -> Graph:
        """
        Apply all N3 rules in directory to data graph using EYE reasoner.
        Returns: Graph with inferred triples (skolemized)
        """
        self.logger.info(f"Applying N3 rules from {rules_dir}")
        
        # Find all N3 rule files
        rule_files = sorted(rules_dir.glob("*.n3"))
        if not rule_files:
            raise FileNotFoundError(f"No .n3 files found in {rules_dir}")
        
        self.logger.info(f"Found {len(rule_files)} rule files:")
        for rule_file in rule_files:
            self.logger.info(f"  - {rule_file.name}")
        
        # Create temporary files for input data and query
        with tempfile.NamedTemporaryFile(mode='w', suffix='.n3', delete=False) as data_file:
            data_graph.serialize(data_file.name, format='n3')
            data_path = data_file.name
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.n3', delete=False) as query_file:
            query_file.write(self.config.n3_query_all)
            query_path = query_file.name
        
        try:
            # Build EYE command: eye <data> <rules> --query <query> --nope (no proof)
            cmd = [self.config.eye_path, data_path]
            cmd.extend([str(f) for f in rule_files])
            cmd.extend(['--query', query_path, '--nope'])  # --nope disables proof output
            
            self.logger.info(f"Running EYE: {' '.join(cmd)}")
            
            # Execute EYE reasoner
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout
            )
            
            if result.returncode != 0:
                self.logger.error(f"EYE failed: {result.stderr}")
                raise RuntimeError(f"EYE reasoner failed: {result.stderr}")
            
            # Parse results
            result_graph = Graph()
            result_graph.parse(data=result.stdout, format='n3')
            
            self.logger.info(f"EYE output {len(result_graph)} total triples")
            
            # Filter out EYE reasoning metadata (keep only domain triples)
            filtered_graph = Graph()
            for prefix, namespace in result_graph.namespaces():
                filtered_graph.bind(prefix, namespace)
            
            # Keep only triples in our namespaces (tb, abox, odrl, dqv)
            for s, p, o in result_graph:
                # Skip EYE metadata triples
                if 'eyereasoner' in str(s) or 'eyereasoner' in str(p):
                    continue
                if str(p) in ['http://www.w3.org/2000/10/swap/reason#',
                              'http://www.w3.org/2004/06/rei#']:
                    continue
                if 'reason#' in str(p) or 'rei#' in str(p):
                    continue
                
                filtered_graph.add((s, p, o))
            
            self.logger.info(f"Filtered to {len(filtered_graph)} domain triples")
            
            # Skolemize blank nodes for clean URIs
            result_graph = skolemize_blank_nodes(filtered_graph)
            
            return result_graph
            
        finally:
            # Clean up temp files
            Path(data_path).unlink(missing_ok=True)
            Path(query_path).unlink(missing_ok=True)


# ============================================================================
# SEMANTIC N3 PLANNER
# ============================================================================

class SemanticN3Planner:
    """
    N3-based planner using SEMANTIC properties for parameter binding.
    
    Instead of flat hasInput, uses:
        - tb:hasFilePath for file paths
        - tb:hasTimestampAttribute for timestamp columns
        - tb:hasTargetAttribute for quality metrics
        - tb:hasPreviousResult for operation chaining
        - odrl:constraint blocks for structured constraints
    """
    
    def __init__(self, sdm_path: str, config: Optional[N3PlannerConfig] = None):
        self.config = config or N3PlannerConfig()
        self.sdm_path = Path(sdm_path)
        self.logger = logging.getLogger(f"{__name__}.SemanticN3Planner")
        
        # Load SDM
        self.logger.info(f"Loading SDM from {self.sdm_path}")
        self.sdm = Graph()
        self.sdm.parse(self.sdm_path, format='turtle')
        
        # Bind namespaces
        self.sdm.bind("tb", tb)
        self.sdm.bind("tbox", tbox)
        self.sdm.bind("abox", abox)
        self.sdm.bind("odrl", odrl)
        self.sdm.bind("dqv", dqv)
        
        self.logger.info(f"Loaded {len(self.sdm)} triples from SDM")
        
        # Initialize reasoner
        self.reasoner = EYEReasoner(self.config)
        
        # Rules directory
        script_dir = Path(__file__).parent
        self.rules_dir = script_dir / self.config.rules_directory
        
        if not self.rules_dir.exists():
            raise FileNotFoundError(f"Rules directory not found: {self.rules_dir}")
        
        self.logger.info(f"Using rules directory: {self.rules_dir}")
    
    def generate_policy_checkers(self, data_product_name: str) -> Graph:
        """
        Generate PolicyChecker graphs for a data product using semantic N3 rules.
        
        Returns: Graph with PolicyChecker operations using semantic properties
        """
        self.logger.info(f"Generating PolicyCheckers for: {data_product_name}")
        
        # Find data product URI
        dp_uri = abox[data_product_name]
        
        # Check if data product exists
        if (dp_uri, RDF.type, None) not in self.sdm:
            raise ValueError(f"Data product not found: {data_product_name}")
        
        # Apply N3 rules with semantic properties
        result_graph = self.reasoner.apply_rules(self.sdm, self.rules_dir)
        
        # Extract PolicyCheckers for this data product
        pc_graph = Graph()
        for prefix, namespace in self.sdm.namespaces():
            pc_graph.bind(prefix, namespace)
        
        # Find all PolicyCheckers for this data product
        pc_found = False
        for pc_uri in result_graph.subjects(tb.validates, dp_uri):
            pc_found = True
            self.logger.info(f"Found PolicyChecker: {pc_uri}")
            
            # Add all triples about this PolicyChecker
            for p, o in result_graph.predicate_objects(pc_uri):
                pc_graph.add((pc_uri, p, o))
            
            # Follow operation chain and add all operations
            self._add_operation_chain(result_graph, pc_graph, pc_uri)
        
        if not pc_found:
            self.logger.warning(f"No PolicyChecker found for {dp_uri}")
            # Debug: show what was inferred
            self.logger.info("Looking for tb:PolicyChecker...")
            for s in result_graph.subjects(RDF.type, tb.PolicyChecker):
                self.logger.info(f"  Found PolicyChecker: {s}")
                for p, o in result_graph.predicate_objects(s):
                    self.logger.info(f"    {p} -> {o}")
            
            self.logger.info("Looking for tb:Operation...")
            for s in result_graph.subjects(RDF.type, tb.Operation):
                self.logger.info(f"  Found Operation: {s}")
                break
            
            self.logger.info("Sample of filtered triples (tb/abox namespace):")
            count = 0
            for s, p, o in result_graph:
                if str(tb) in str(s) or str(abox) in str(s):
                    if count < 30:
                        self.logger.info(f"  {s} {p} {o}")
                        count += 1
        
        self.logger.info(f"Generated PolicyChecker with {len(pc_graph)} triples")
        
        return pc_graph
    
    def _add_operation_chain(self, source_graph: Graph, target_graph: Graph, pc_uri: URIRef):
        """Add all operations in chain to target graph"""
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
                
                # If object is an input node (blank node or genid), add its triples
                if isinstance(o, (URIRef, BNode)) and p == tb.hasInput:
                    for ip, io in source_graph.predicate_objects(o):
                        target_graph.add((o, ip, io))
            
            # Move to next operation
            current = source_graph.value(current, tb.nextStep)
    
    def save_policy_checkers(self, pc_graph: Graph, output_path: str):
        """Save PolicyChecker graph to file"""
        output_file = Path(output_path)
        pc_graph.serialize(output_file, format='turtle')
        self.logger.info(f"Saved PolicyChecker to {output_file}")
    
    def merge_policy_checkers_to_sdm(self, pc_graph: Graph):
        """Merge PolicyChecker graph back into main SDM (single source of truth)"""
        self.logger.info(f"Merging PolicyCheckers to main SDM: {self.sdm_path}")
        
        original_count = len(self.sdm)
        
        # Add all PolicyChecker triples to SDM
        for s, p, o in pc_graph:
            self.sdm.add((s, p, o))
        
        new_count = len(self.sdm)
        triples_added = new_count - original_count
        
        self.logger.info(f"Added {triples_added} PolicyChecker triples to SDM")
        self.logger.info(f"SDM now has {new_count} total triples")
        
        # Save updated SDM
        self.sdm.serialize(self.sdm_path, format='turtle')
        self.logger.info(f"✓ Updated SDM saved to {self.sdm_path}")


# ============================================================================
# CLI
# ============================================================================

def main():
    """CLI for semantic N3 planner"""
    import sys
    
    if len(sys.argv) != 3:
        print("Usage: python planner_n3_semantic.py <sdm_file> <data_product_name>")
        print("Example: python planner_n3_semantic.py sdm.ttl UPENN-GBM_clinical_info_v21_timestampcsv")
        sys.exit(1)
    
    sdm_file = sys.argv[1]
    dp_name = sys.argv[2]
    
    # Create planner
    planner = SemanticN3Planner(sdm_file)
    
    # Generate PolicyCheckers
    pc_graph = planner.generate_policy_checkers(dp_name)
    
    print(f"\n✓ Generated PolicyChecker with {len(pc_graph)} triples")

    # Save to file (required for test script)
    output_filename = f"policy_checker_{dp_name}_semantic.ttl"
    output_path = str(Path(__file__).parent / output_filename)
    planner.save_policy_checkers(pc_graph, output_path)
    print(f"✓ Saved PolicyChecker to {output_path}")
    
    # Merge PolicyCheckers to main SDM (single source of truth)
    print(f"🔄 Merging PolicyCheckers to main SDM...")
    planner.merge_policy_checkers_to_sdm(pc_graph)
    print(f"✅ PolicyCheckers merged to: {sdm_file}")


if __name__ == "__main__":
    main()
