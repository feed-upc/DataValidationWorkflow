# Federated Data Validation Framework - Semantic Approach

**A Policy-Driven Validation Framework using N3 Logic Rules and Composed UDF Execution**

This framework validates federated data products against quality, privacy, and correctness policies using semantic reasoning and efficient execution pipelines.

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Step-by-Step Example: Policy p6 (Timeliness)](#step-by-step-example-policy-p6-timeliness)
4. [Components](#components)
5. [Quick Start](#quick-start)
6. [Advanced Usage](#advanced-usage)

---

## Overview

### What Problem Does This Solve?

In federated data environments, data products must comply with various policies:
- **Quality policies**: Data freshness, completeness, accuracy
- **Privacy policies**: K-anonymity, differential privacy
- **Correctness policies**: Schema compliance, value constraints

**Challenge**: Different policies require different validation operations, and manually coding each validation is error-prone and not scalable.

**Solution**: This framework uses **N3 logic rules** to automatically transform high-level policies (ODRL) into executable validation pipelines.

### Key Features

✅ **Semantic reasoning** - N3 rules automatically generate validation code  
✅ **Role-annotated parameters** - No ambiguity in parameter binding  
✅ **Composed UDF execution** - Efficient single-function pipeline execution  
✅ **Policy-driven** - Change policies, get new validations automatically  
✅ **Federated-ready** - Works across distributed data products  

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    FEDERATED DATA PLATFORM                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐  │
│  │ Data Product │      │ Data Product │      │ Data Product │  │
│  │      1       │      │      2       │      │      3       │  │
│  │              │      │              │      │              │  │
│  │ - Data       │      │ - Data       │      │ - Data       │  │
│  │ - Metadata   │      │ - Metadata   │      │ - Metadata   │  │
│  │ - Policy     │      │ - Policy     │      │ - Policy     │  │
│  └──────┬───────┘      └──────┬───────┘      └──────┬───────┘  │
│         │                     │                     │           │
│         └─────────────────────┴─────────────────────┘           │
│                              │                                   │
└──────────────────────────────┼───────────────────────────────────┘
                               │
                               ▼
        ┌──────────────────────────────────────────┐
        │   VALIDATION FRAMEWORK (This Project)    │
        └──────────────────────────────────────────┘
                               │
                ┌──────────────┴───────────────┐
                │                              │
                ▼                              ▼
        ┌───────────────┐            ┌─────────────────┐
        │   PLANNER     │            │    EXECUTOR     │
        │  (N3 Rules)   │            │  (Composed UDF) │
        └───────────────┘            └─────────────────┘
                │                              │
                │   PolicyChecker              │
                │   (Operations Chain)         │
                └──────────────►───────────────┘
                                               │
                                               ▼
                                    ┌─────────────────────┐
                                    │  Validation Result  │
                                    │  (Pass/Fail + Data) │
                                    └─────────────────────┘
```

### Two-Phase Approach

#### Phase 1: PLANNER (Planning Time)
- **Input**: Policy (ODRL/DQV) + Semantic Data Model (SDM)
- **Process**: N3 logic rules transform policy into operation chain
- **Output**: PolicyChecker (executable operation graph)

#### Phase 2: EXECUTOR (Execution Time)
- **Input**: PolicyChecker + Code Metadata
- **Process**: Compose operations into single UDF, execute
- **Output**: Validation result (data or boolean)

---

## Step-by-Step Example: Policy p6 (Timeliness)

Let's walk through **p6**, a timeliness/freshness policy that checks if data is no older than 30 minutes.

### Input: Policy p6 (JSON-LD)

```json
{
  "@id": "ab:p6",
  "@type": "dqv:QualityPolicy",
  "odrl:permission": [{
    "@type": "odrl:Permission",
    "odrl:target": { "@id": "ab:Patient_Data" },
    "odrl:action": { "@id": "odrl:distribute" },
    "odrl:constraint": [{
      "@type": "odrl:Constraint",
      "odrl:leftOperand": [{
        "@id": "ab:DataAge",
        "@type": "dqv:QualityMeasurement",
        "dqv:computedOn": [{ "@id": "ab:timestamp" }],
        "dqv:isMeasurementOf": [{
          "@id": "ab:Freshness",
          "dqv:inDimension": [{ "@id": "ab:Timeliness" }]
        }]
      }],
      "odrl:operator": "odrl:lteq",
      "odrl:rightOperand": { "@value": "30", "@type": "xsd:integer" },
      "odrl:unit": { "@id": "https://qudt.org/vocab/unit/MIN" }
    }]
  }]
}
```

**What this says in plain English:**
> "Data can be distributed only if the DataAge (computed from timestamp) is less than or equal to 30 minutes."

---

### Step 1: Planner Phase - N3 Rule Firing

The planner loads the policy and fires N3 rules to generate a PolicyChecker.

#### Rule 1: LoadData.n3 (Fires First)

**Trigger**: Policy exists + Data product has static file access

**N3 Rule**:
```n3
{
    ?dp tb:hasDC ?dc .
    ?dc tb:hasPolicy ?policy .
    ?dp tb:hasTA ?ta .
    ?ta tb:typeAcces ?access .
    ?access rdfs:label ab:Static ;
            tb:path ?path .
}
=>
{
    _:pc a tb:PolicyChecker ;
         tb:validates ?dp ;
         tb:accordingTo ?policy ;
         tb:nextStep _:loadOp .
    
    _:loadOp a tb:Operation ;
             tb:hasAbstract ab:LoadData ;
             tb:hasInput [
                 tb:value ?path ;
                 tb:role "filePath"
             ] ;
             tb:hasOutput ab:data .
}
```

**What it does**:
1. Finds the data product's file path from technical aspects
2. Creates a PolicyChecker
3. Creates a LoadData operation with role-annotated input:
   - `tb:role "filePath"` → will bind to parameter `p` in code template
   - `tb:value ?path` → actual file path value

**Generated Output**:
```turtle
ab:genid-abc123 a tb:PolicyChecker ;
    tb:validates ab:Patient_Data ;
    tb:accordingTo ab:p6 ;
    tb:nextStep ab:genid-op1 .

ab:genid-op1 a tb:Operation ;
    tb:hasAbstract ab:LoadData ;
    tb:hasInput [
        tb:value "DataProductLayer/DataProduct/Data/clinical_info.csv" ;
        tb:role "filePath"
    ] ;
    tb:hasOutput ab:data .
```

---

#### Rule 2: ConstraintPropagation.n3 (Fires Second)

**Trigger**: Policy constraint references semantic feature (ab:timestamp)

**N3 Rule**:
```n3
{
    ?policy a dqv:QualityPolicy ;
            odrl:permission ?permission .
    ?permission odrl:constraint ?constraint .
    ?constraint odrl:leftOperand ?measurement .
    ?measurement dqv:computedOn ?semanticFeature .
    ?mapping tb:mfrom ?physicalAttr ;
             tb:mto ?semanticFeature .
}
=>
{
    ?permission tb:physicalAttribute ?physicalAttr .
}
```

**What it does**:
1. Finds that DataAge is computed on semantic feature `ab:timestamp`
2. Looks up mapping: `ab:timestamp` → physical attribute `"timestamp"` (column name)
3. Adds `tb:physicalAttribute "timestamp"` to permission for later rules to use

**Why this matters**: Policies use semantic concepts (age, gender), but code needs physical column names (Age_at_scan, Gender_value). This rule bridges the gap.

---

#### Rule 3: Timeliness.n3 (Fires Third)

**Trigger**: Quality policy with permission + freshness constraint + timestamp attribute resolved

**N3 Rule**:
```n3
{
    ?pc tb:nextStep ?loadOp .
    ?loadOp a tb:Operation ;
            tb:hasAbstract ab:LoadData .
    ?policy a dqv:QualityPolicy ;
            odrl:permission ?permission .
    ?permission odrl:constraint ?constraint ;
                tb:physicalAttribute ?physicalTimestamp .
    ?constraint odrl:leftOperand ?dataAgeMeasurement ;
                odrl:operator ?operator ;
                odrl:rightOperand ?maxAge .
}
=>
{
    _:freshnessOp a tb:Operation ;
                  tb:hasAbstract ab:CheckFreshness ;
                  tb:hasInput [
                      tb:value ?physicalTimestamp ;
                      tb:role "timestampAttribute"
                  ] ;
                  tb:hasInput [
                      tb:value ab:data ;
                      tb:role "previousResult"
                  ] ;
                  tb:hasOutput ab:data ;
                  tb:nextStep _:validationOp .
    
    ?loadOp tb:nextStep _:freshnessOp .
    
    _:validationOp a tb:Operation ;
                   tb:hasAbstract odrl:Constraint ;
                   tb:hasInput [
                       tb:value ab:data ;
                       tb:role "previousResult"
                   ] ;
                   tb:hasInput [
                       tb:value ?maxAge ;
                       tb:role "threshold"
                   ] ;
                   tb:hasInput [
                       tb:value ?operator ;
                       tb:role "operator"
                   ] ;
                   tb:hasOutput ab:data ;
                   tb:isTerminal true .
}
```

**What it does**:
1. Chains TWO operations from LoadData:
   - **CheckFreshness**: Filters data where timestamp age < 30 minutes
   - **Constraint**: Validates the result (always true for <= threshold)

2. Creates role-annotated inputs for CheckFreshness:
   - `tb:role "timestampAttribute"` → `"timestamp"` column
   - `tb:role "previousResult"` → data from LoadData

3. Creates role-annotated inputs for Constraint:
   - `tb:role "previousResult"` → data from CheckFreshness
   - `tb:role "threshold"` → `30`
   - `tb:role "operator"` → `"odrl:lteq"`

**Generated Output**:
```turtle
ab:genid-op1 tb:nextStep ab:genid-op2 .

ab:genid-op2 a tb:Operation ;
    tb:hasAbstract ab:CheckFreshness ;
    tb:hasInput [
        tb:value "timestamp" ;
        tb:role "timestampAttribute"
    ] ;
    tb:hasInput [
        tb:value ab:data ;
        tb:role "previousResult"
    ] ;
    tb:hasOutput ab:data ;
    tb:nextStep ab:genid-op3 .

ab:genid-op3 a tb:Operation ;
    tb:hasAbstract odrl:Constraint ;
    tb:hasInput [
        tb:value ab:data ;
        tb:role "previousResult"
    ] ;
    tb:hasInput [
        tb:value 30 ;
        tb:role "threshold"
    ] ;
    tb:hasInput [
        tb:value "odrl:lteq" ;
        tb:role "operator"
    ] ;
    tb:hasOutput ab:data ;
    tb:isTerminal true .
```

---

### Step 2: Executor Phase - Composed UDF Generation

The executor receives the PolicyChecker and composes all operations into a single function.

#### Execution Process

**Input**: PolicyChecker with 3 operations chained via `tb:nextStep`

**Step 1: Collect Operations**

The executor walks the operation chain:

```
Operation 1: LoadData
  - Abstract: ab:LoadData
  - Template: pandas.read_csv(p)
  - Bindings: { p: "DataProductLayer/DataProduct/Data/clinical_info.csv" }

Operation 2: CheckFreshness
  - Abstract: ab:CheckFreshness
  - Template: data[data[ts_attr].apply(lambda x: (datetime.now() - pd.to_datetime(x)).total_seconds() / 60 < 30)]
  - Bindings: { ts_attr: "timestamp", data: ab:data }

Operation 3: Constraint
  - Abstract: odrl:Constraint
  - Template: data <= threshold
  - Bindings: { data: ab:data, threshold: 30, operator: "odrl:lteq" }
```

**Step 2: Compose into Single Function**

The executor generates a single Python function:

```python
def composed_pipeline(initial_data=None):
    # Step 1: ab:LoadData
    result_0 = pandas.read_csv("DataProductLayer/DataProduct/Data/clinical_info.csv")

    # Step 2: ab:CheckFreshness
    result_1 = result_0[result_0["timestamp"].apply(lambda x: (datetime.now() - pd.to_datetime(x)).total_seconds() / 60 < 30)]

    # Step 3: odrl:Constraint
    result_2 = result_1 <= 30

    return result_2
```

**Key details**:
- Each operation becomes a step in the pipeline
- Results chain: `result_0` → `result_1` → `result_2`
- Parameter substitution: `p` → file path, `ts_attr` → `"timestamp"`, `data` → `result_0`
- Single execution, clear dataflow

**Step 3: Execute**

```python
exec_env = {'pandas': pd, 'datetime': datetime, 'pd': pd}
exec(composed_code, exec_env)
result = exec_env['composed_pipeline'](None)
```

**Output**: Filtered dataframe with only fresh data (< 30 minutes old)

---

### Complete Flow Diagram for p6

```
┌─────────────────────────────────────────────────────────────────────┐
│                          POLICY p6 (Input)                           │
│  "Data age computed from timestamp must be <= 30 minutes"           │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │   PLANNER (N3 Rules)   │
                    └────────────────────────┘
                                 │
            ┌────────────────────┼────────────────────┐
            ▼                    ▼                    ▼
   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
   │ Rule 1: LoadData│  │ Rule 2: Const.  │  │ Rule 3: Time.   │
   │  - Create PC    │  │  Propagation    │  │  - Chain Ops    │
   │  - Load CSV     │  │  - Map Feature  │  │  - Freshness    │
   │    operation    │  │    to Attribute │  │  - Constraint   │
   └─────────────────┘  └─────────────────┘  └─────────────────┘
            │                    │                    │
            └────────────────────┴────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │   POLICYCHECKER (TTL)  │
                    │  3 operations chained  │
                    └────────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │  EXECUTOR (Compose)    │
                    └────────────────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        ▼                        ▼                        ▼
┌──────────────┐      ┌──────────────────┐      ┌────────────────┐
│ Op1: LoadData│ ───► │ Op2: CheckFresh. │ ───► │ Op3: Constraint│
│ result_0 = ..│      │ result_1 = ...   │      │ result_2 = ... │
└──────────────┘      └──────────────────┘      └────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │  composed_pipeline()   │
                    │  Single function with  │
                    │  all 3 operations      │
                    └────────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │   EXECUTION (Python)   │
                    └────────────────────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │  RESULT: Fresh data    │
                    │  (DataFrame filtered)  │
                    └────────────────────────┘
```

---

## Components

### 1. Planner (`planner/`)

**Purpose**: Transform policies into executable PolicyCheckers using N3 logic

**Key Files**:
- `planner_n3_semantic.py` - Main planner implementation
- `planner_n3_semantic.ipynb` - Interactive notebook for testing
- `rules_n3_semantic/` - N3 transformation rules
  - `LoadData.n3` - Initialize data loading operation
  - `ConstraintPropagation.n3` - Map semantic features to physical attributes
  - `Timeliness.n3` - Transform freshness policies
  - `QualityMeasurement.n3` - Transform completeness/accuracy policies

**Usage**:
```python
from planner_n3_semantic import SemanticN3Planner

planner = SemanticN3Planner(
    sdm_file="path/to/semantic_data_model.ttl",
    data_product_id="UPENN-GBM_clinical_info"
)

policy_checker_graph = planner.generate_policy_checker()
policy_checker_graph.serialize("output.ttl", format="turtle")
```

**How it works**:
1. Loads Semantic Data Model (SDM) containing policies and mappings
2. Invokes EYE reasoner with N3 rules
3. Rules fire in sequence, transforming ODRL → Operations
4. Returns PolicyChecker graph (RDF/Turtle)

---

### 2. Executor (`executor/`)

**Purpose**: Execute PolicyCheckers using composed UDF approach with execution metadata tracking and SDM integration

**Key Files**:
- `executor_semantic.py` - Main executor implementation
- `executor_semantic.ipynb` - Interactive notebook for testing
- `code_metadata_with_roles.json` - Code templates with parameter roles
- `test_semantic_workflow.py` - End-to-end test suite
- `inspect_validation_reports.py` - Utility to inspect validation reports
- `query_sdm_reports.py` - Query validation reports from SDM
- `example_validation_reports.py` - Complete workflow example
- `parameter_binding_semantic.n3` - N3 rules for parameter binding (if needed)

**Usage**:
```python
from executor_semantic import SemanticN3Executor
from rdflib import Graph, Namespace

# Load PolicyChecker
pc_graph = Graph()
pc_graph.parse("policy_checker.ttl", format="turtle")

# Create executor (composed UDF mode by default, with annotation)
executor = SemanticN3Executor(
    pc_graph=pc_graph,
    code_metadata_file="code_metadata_with_roles.json",
    annotate_graph=True  # Enable validation report generation
)

# Execute
tb = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
pc_uri = list(pc_graph.subjects(RDF.type, tb.PolicyChecker))[0]
result, report_uri = executor.execute(pc_uri)

# Save annotated graph with validation reports
executor.save_annotated_graph("policy_checker_with_reports.ttl")

# Merge validation reports back to SDM
from executor_semantic import merge_validation_reports_to_sdm

merged_sdm = merge_validation_reports_to_sdm(
    pc_graph=pc_graph,
    sdm_file="path/to/sdm_with_pcs.ttl",
    output_sdm_file="path/to/sdm_with_pcs_with_reports.ttl"
)
```

**How it works**:
1. Collects all operations in chain (walks `tb:nextStep`)
2. Extracts role-annotated parameters from each operation
3. Composes operations into single `composed_pipeline()` function
4. Chains results: `result_0` → `result_1` → `result_2`, etc.
5. Tracks execution time and metadata
6. Executes composed function in Python environment
7. **Creates validation report node** with:
   - Execution status (PASSED/FAILED/COMPLETED)
   - Execution duration (seconds)
   - Start/end timestamps
   - Result metadata (type, shape, values)
   - Execution mode (COMPOSED_UDF)
8. **Merges reports back to SDM**:
   - Matches PolicyCheckers by policy and data product
   - Links reports to SDM PolicyCheckers
   - Preserves full SDM content
   - Persists validation history
9. Returns validation result and report URI

---

### 3. Code Metadata (`executor/code_metadata_with_roles.json`)

**Purpose**: Maps abstract operations to concrete Python code templates

**Structure**:
```json
{
  "@context": { ... },
  "@graph": [
    {
      "@id": "ab:Imp1",
      "tb:abstractOperation": "ab:LoadData",
      "tb:hasCode": [{
        "tb:code": "pandas.read_csv(p)",
        "tb:hasParameter": [
          { "tb:name": "p", "tb:role": "filePath" }
        ]
      }],
      "tb:dependsOn": [
        { "tb:name": "pandas" }
      ]
    }
  ]
}
```

**How roles work**:
- Each code template has parameters (e.g., `p`, `data`, `ts_attr`)
- Each parameter has a role (e.g., `"filePath"`, `"previousResult"`)
- Executor matches roles from PolicyChecker to bind correct values

---

### 4. N3 Rules (`planner/rules_n3_semantic/`)

**Purpose**: Declarative transformation rules for policy → operations

**Rule Structure**:
```n3
# Condition (IF part)
{
    # Pattern matching on policy structure
    ?policy a dqv:QualityPolicy .
    ?policy odrl:permission ?permission .
    # ... more patterns ...
}
=>
# Conclusion (THEN part)
{
    # Generate operations
    _:op a tb:Operation ;
         tb:hasAbstract ab:SomeOperation ;
         tb:hasInput [
             tb:value ?someValue ;
             tb:role "someRole"
         ] .
}
```

**Available Rules**:
- **LoadData.n3**: Creates initial data loading operation
- **ConstraintPropagation.n3**: Maps semantic concepts to physical attributes
- **Timeliness.n3**: Handles freshness/staleness policies
- **QualityMeasurement.n3**: Handles completeness, accuracy policies

---

## Quick Start

### Prerequisites

```bash
# Install dependencies
pip install -r requirements.txt

# Install EYE reasoner (N3 logic engine)
# Option 1: npm (recommended)
npm install -g eye-js

# Option 2: Download binary from
# https://github.com/eyereasoner/eye
```

### Run the Complete Workflow

```bash
cd Connector/ValidationFramework

# Step 1: Generate PolicyChecker (Planner)
cd planner
python planner_n3_semantic.py \
    ../../../FederatedComputationalGovernance/SemanticDataModel/sdm.ttl \
    UPENN-GBM_clinical_info_v21_timestampcsv

# Step 2: Execute validation (Executor)
cd ../executor
python test_semantic_workflow.py
```

### Expected Output

```
============================================================
COMPOSING UDF PIPELINE
============================================================

  Step 1: http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#LoadData
    Template: pandas.read_csv(p)
  Step 2: http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#CheckFreshness
    Template: data[data[ts_attr].apply(lambda x: (datetime.now() - pd.to_datetime(x)).total_seconds() / 60 < 30)]
  Step 3: http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#Constraint
    Template: data <= threshold

============================================================
COMPOSED UDF:
============================================================
def composed_pipeline(initial_data=None):
    # Step 1: ab:LoadData
    result_0 = pandas.read_csv("DataProductLayer/DataProduct/Data/clinical_info.csv")

    # Step 2: ab:CheckFreshness
    result_1 = result_0[result_0["timestamp"].apply(lambda x: (datetime.now() - pd.to_datetime(x)).total_seconds() / 60 < 30)]

    # Step 3: odrl:Constraint
    result_2 = result_1 <= 30

    return result_2

============================================================

EXECUTING COMPOSED UDF...
✓ Composed UDF executed successfully

✓ p6 (Timeliness) PASSED
```

---

## Advanced Usage

### Adding New Policies

1. **Define policy in JSON-LD** (add to `FederatedComputationalGovernance/ComputationalCatalogues/`)
2. **Create N3 rule** (if new policy type) in `planner/rules_n3_semantic/`
3. **Add code template** (if new operation) in `executor/code_metadata_with_roles.json`
4. **Run planner** to generate PolicyChecker
5. **Run executor** to validate

### Testing Individual Components

#### Test Planner Only
```bash
cd planner
jupyter notebook planner_n3_semantic.ipynb
# Interactive: test rules, inspect PolicyChecker
```

#### Test Executor Only
```bash
cd executor
jupyter notebook executor_semantic.ipynb
# Interactive: test operation composition, debug bindings
```

#### Test Specific Policy
```python
# In test_semantic_workflow.py
def test_policy(pc_uri, policy_name, exec_graph):
    executor = SemanticN3Executor(exec_graph, metadata_file, annotate_graph=True)
    result, report_uri = executor.execute(pc_uri)
    print(f"✓ {policy_name} PASSED" if result else f"✗ {policy_name} FAILED")
    return result, report_uri
```

#### Inspect Validation Reports
```bash
cd executor
# View detailed reports
python inspect_validation_reports.py ../planner/policy_checker_with_reports.ttl

# Compare multiple reports
python inspect_validation_reports.py ../planner/policy_checker_with_reports.ttl --compare
```

### Debugging

Enable verbose output:
```python
# In executor_semantic.py
import logging
logging.basicConfig(level=logging.DEBUG)
```

Inspect PolicyChecker graph:
```python
from rdflib import Graph

pc_graph = Graph()
pc_graph.parse("policy_checker.ttl", format="turtle")

# Show all operations
for op in pc_graph.subjects(RDF.type, tb.Operation):
    print(f"Operation: {op}")
    abstract = pc_graph.value(op, tb.hasAbstract)
    print(f"  Abstract: {abstract}")
    
    for input_node in pc_graph.objects(op, tb.hasInput):
        role = pc_graph.value(input_node, tb.role)
        value = pc_graph.value(input_node, tb.value)
        print(f"  Input: role={role}, value={value}")
```

---

## Validation Reports

The executor automatically generates **validation report nodes** in the RDF graph that capture execution metadata. These reports provide traceability and auditing capabilities.

### Report Structure

Each validation report includes:

```turtle
abox:ValidationReport-abc123 a tb:ValidationReport ;
    # Provenance
    prov:wasGeneratedBy <PolicyChecker URI> ;
    
    # Temporal metadata
    prov:startedAtTime "2025-12-16T13:43:00.224549"^^xsd:dateTime ;
    prov:endedAtTime "2025-12-16T13:43:00.410389"^^xsd:dateTime ;
    tb:executionDuration "0.185840"^^xsd:float ;
    
    # Execution metadata
    tb:validationStatus "PASSED" ;
    tb:executionMode "COMPOSED_UDF" ;
    
    # Result metadata
    tb:resultType "DataFrame" ;
    tb:resultRowCount 150 ;
    tb:resultColumnCount 10 ;
    tb:resultColumn "col1", "col2", ... ;
    
    # Or for scalar results
    tb:resultValue "100.0"^^xsd:float .
```

### Status Values

- **PASSED**: Validation succeeded (e.g., data passed quality checks)
- **FAILED**: Validation failed (e.g., no data met criteria)
- **COMPLETED**: Execution completed but status is ambiguous

### Querying Reports

```python
from rdflib import Graph, Namespace

# Load annotated graph
g = Graph()
g.parse("policy_checker_with_reports.ttl", format="turtle")

# Find all reports
tb = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
reports = list(g.subjects(RDF.type, tb.ValidationReport))

# Get report details
for report in reports:
    status = g.value(report, tb.validationStatus)
    duration = g.value(report, tb.executionDuration)
    print(f"Status: {status}, Duration: {duration}s")
```

### Use Cases

1. **Audit Trail**: Track when and how validations were executed
2. **Performance Monitoring**: Compare execution times across policies
3. **Quality Metrics**: Aggregate validation results over time
4. **Debugging**: Inspect what data shapes were produced
5. **Compliance**: Demonstrate validation procedures were followed

---

## Key Concepts

### Role-Annotated Parameters

**Problem**: Multiple parameters of the same type (e.g., two strings) are ambiguous.

**Solution**: Each parameter has a role:
```turtle
tb:hasInput [
    tb:value "timestamp" ;
    tb:role "timestampAttribute"
] ;
tb:hasInput [
    tb:value "age" ;
    tb:role "targetAttribute"
] .
```

Executor maps roles to code parameters:
- `"timestampAttribute"` → `ts_attr` in code
- `"targetAttribute"` → `attr` in code

### Composed UDF Execution

**Benefits**:
- **Single execution**: No loop overhead
- **Clear dataflow**: See entire pipeline at once
- **Optimizable**: Can apply transformations to composed function
- **Serializable**: Can save/load composed functions

**Comparison**:

Sequential (old):
```python
result = load_data(path)
result = check_freshness(result, ts_attr)
result = validate_constraint(result, threshold)
```

Composed (new):
```python
def composed_pipeline(initial_data=None):
    result_0 = pandas.read_csv(path)
    result_1 = result_0[result_0[ts_attr]...]
    result_2 = result_1 <= threshold
    return result_2

result = composed_pipeline()
```

### N3 Logic Rules

**Declarative transformation**: Rules specify WHAT to transform, not HOW.

**Example**:
```n3
{ ?x a Person . ?x hasAge ?age . ?age >= 18 } 
=> 
{ ?x a Adult } .
```

**Benefits**:
- Separates policy logic from execution
- Rules are composable and reusable
- Changes to policies don't require code changes

---

## Repository Structure

```
Connector/ValidationFramework/
├── README.md (this file)
├── requirements.txt
│
├── planner/
│   ├── planner_n3_semantic.py          # Main planner
│   ├── planner_n3_semantic.ipynb       # Interactive notebook
│   ├── rules_n3_semantic/              # N3 transformation rules
│   │   ├── LoadData.n3
│   │   ├── ConstraintPropagation.n3
│   │   ├── Timeliness.n3
│   │   └── QualityMeasurement.n3
│   └── policy_checker_*.ttl            # Generated PolicyCheckers
│
├── executor/
│   ├── executor_semantic.py            # Main executor
│   ├── executor_semantic.ipynb         # Interactive notebook
│   ├── test_semantic_workflow.py       # End-to-end tests
│   ├── code_metadata_with_roles.json   # Code templates
│   └── parameter_binding_semantic.n3   # Binding rules (optional)
│
└── experiments/                        # Research experiments
```

---

## Contributing

### Adding a New Policy Type

1. **Create N3 rule** in `planner/rules_n3_semantic/YourPolicy.n3`
2. **Define operation abstract** in ontology (e.g., `ab:YourOperation`)
3. **Add code implementation** in `executor/code_metadata_with_roles.json`
4. **Test with planner** using `planner_n3_semantic.ipynb`
5. **Test with executor** using `executor_semantic.ipynb`
6. **Add end-to-end test** in `test_semantic_workflow.py`

### Rule Writing Guidelines

- **Use role-annotated inputs**: `tb:hasInput [ tb:role "...", tb:value ... ]`
- **Chain operations**: Use `tb:nextStep` for operation sequencing
- **Mark terminals**: Add `tb:isTerminal true` for final operations
- **Document triggers**: Comment what policy patterns trigger the rule

---

## References

- **ODRL**: Open Digital Rights Language - https://www.w3.org/TR/odrl-model/
- **DQV**: Data Quality Vocabulary - https://www.w3.org/TR/vocab-dqv/
- **N3 Logic**: Notation3 - https://www.w3.org/TeamSubmission/n3/
- **EYE Reasoner**: https://github.com/eyereasoner/eye

---

## License

[Your License Here]

## Contact

[Your Contact Information]

---

**Last Updated**: December 16, 2025  
**Version**: 2.0.0 (Semantic + Composed UDF)
