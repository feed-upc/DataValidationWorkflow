import types
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from rdflib import Graph, Namespace, URIRef
import os, sys
import logging
from typing import Dict, List, Optional
import json
import os, sys
import logging
from typing import Dict, List, Optional
import json
import subprocess
import shutil
from pathlib import Path

tbox = Namespace('http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#')
abox = Namespace('http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#')

base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
sys.path.append(base_dir)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

app = FastAPI(title="Validation Framework API")

# Constants
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get('PROJECT_ROOT', os.path.join(BASE_DIR, '..', '..', '..'))
SDM_PATH = os.environ.get('SDM_PATH', os.path.join(PROJECT_ROOT, 'FederatedComputationalGovernance', 'SemanticDataModel', 'sdm.ttl'))

# Namespaces
TB = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#")
AB = Namespace("http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#")
ODRL = Namespace("http://www.w3.org/ns/odrl/2/")

class ValidationFramework:
    def __init__(self):
        self.sdm = Graph()
        self.load_semantic_model()

    def load_semantic_model(self):
        """Load the semantic data model from TTL file"""
        try:
            self.sdm.parse(SDM_PATH, format='turtle')
            logging.info("Semantic model loaded successfully")
        except Exception as e:
            logging.error(f"Error loading semantic model: {str(e)}")
            raise

    def query_policy_checkers(self, dp: str) -> List[str]:
        """Query policy checkers for a given data product"""
        query = """
        PREFIX tb: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#>
        PREFIX ab: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#>
        PREFIX odrl: <http://www.w3.org/ns/odrl/2/>

        SELECT ?pc ?ds
        WHERE {
            ?pc a tb:PolicyChecker .
            ?pc tb:validates ?dp .
            FILTER (?dp = ab:%s)        
        }
        """

        results = self.sdm.query(query % dp)
        return [str(row.pc).split("#")[1] for row in results]

    def get_validation_reports(self) -> List[Dict]:
        """Get all validation reports"""
        query = """
        PREFIX odrl: <http://www.w3.org/ns/odrl/2/>
        PREFIX tb: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#>
        PREFIX ab: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

        SELECT ?dp ?p ?pc ?result
        WHERE {
            ?pc a tb:PolicyChecker .
            ?pc tb:validates ?dp .
            ?pc tb:accordingTo ?p .
            ?pc tb:nextStep+ ?rep .
            ?rep a tb:Report .
            ?rep tb:result ?result .
        }
        """

        results = self.sdm.query(query)
        return [{
            'data_product': str(row['dp']).split('#')[1],
            'policy': str(row['p']).split('#')[1],
            'policy_checker': str(row['pc']).split('#')[1],
            'result': row['result'].value
        } for row in results]

    def get_data_products(self) -> List[str]:
        """Get all data products with data contracts"""
        query = """
        PREFIX tb: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/tbox#>
        PREFIX ab: <http://www.semanticweb.org/acraf/ontologies/2024/healthmesh/abox#>
        PREFIX odrl: <http://www.w3.org/ns/odrl/2/>

        SELECT DISTINCT ?dp
        WHERE {
            ?dp tb:hasDC ?dc .
        }
        """

        results = self.sdm.query(query)
        return [str(row['dp']).split('#')[1] for row in results]

    def add_policy(self, policy) -> Graph:
        instances = Graph().parse(data=policy, format='json-ld')
        self.sdm += instances
        self.sdm.serialize(destination=SDM_PATH, format='turtle')
        return instances.identifier


validation_framework = ValidationFramework()


@app.get('/test')
def test():
    validation_result = True
    if validation_result:
        return {'status': 'true'}
    else:
        return {'status': 'false'}


@app.get('/data-products')
def get_data_products():
    try:
        products = validation_framework.get_data_products()
        return {
            'data_products': products,
            'count': len(products)
        }
    except Exception as e:
        logging.error(f"Error getting data products: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put('/policies')
async def add_policy(request: Request):
    try:
        policy_data = await request.json()

        # Maybe add SHACL shapes for validation
        policy_id = validation_framework.add_policy(policy_data)

        return {
            'message': 'Policy added successfully',
            'policy_id': policy_id,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        logging.error(f"Error adding policy: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/policy-checkers/{dp_id}')
def get_policy_checkers(dp_id: str):
    try:
        checkers = validation_framework.query_policy_checkers(dp_id)
        return {
            'data_product': dp_id,
            'policy_checkers': checkers,
            'count': len(checkers)
        }
    except Exception as e:
        logging.error(f"Error getting policy checkers: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/validation-reports')
def get_validation_reports():
    try:
        reports = validation_framework.get_validation_reports()
        return {
            'reports': reports,
            'count': len(reports)
        }
    except Exception as e:
        logging.error(f"Error getting validation reports: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post('/generate-validation-service')
async def generate_validation_service(request: Request):
    """
    HTTP endpoint to trigger the end-to-end framework generation process (Federator, Planner, Executor).
    This abstracts away the local shell commands from the frontend GUI.
    """
    try:
        try:
            data = await request.json()
        except:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
            
        rule_id = data.get('rule_id')
        data_product = data.get('data_product', 'Patient_Summary')
        
        if not rule_id:
            raise HTTPException(status_code=400, detail="rule_id is required")
            
        # Context Paths
        project_root = Path(PROJECT_ROOT)
        
        # 1. Update dp1.json to bind this specific rule to the dataset
        dp1_path = project_root / 'DataPlatformLayer' / 'Integration' / 'dp1.json'
        
        with open(dp1_path, "r") as f:
            contract = json.load(f)
            
        backup_path = dp1_path.with_suffix(".json.bak")
        with open(backup_path, "w") as f:
            json.dump(contract, f)
            
        # Backup the SDM so it can be restored after the pipeline run
        sdm_path = Path(SDM_PATH)
        sdm_backup_path = sdm_path.with_suffix(".ttl.bak")
        shutil.copy2(str(sdm_path), str(sdm_backup_path))
            
        contract["policies"] = [f"{rule_id}Rule"]
        with open(dp1_path, "w") as f:
            json.dump(contract, f, indent=4)
            
        export_dir = project_root / 'FederatedComputationalGovernance' / 'ComputationalCatalogues' / 'services'
        export_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. Invoke the framework orchestrator (demo.py) via subprocess to parse SDM and execute
        env = os.environ.copy()
        env["PATH"] = str(project_root / "node_modules/.bin") + os.pathsep + env.get("PATH", "")
        
        cmd = [
            sys.executable, "demo/run_demo.py",
            "--skip-registration",
            "--backend", "gx", 
            "--export-docker", str(export_dir)
        ]
        
        result = subprocess.run(
            cmd, cwd=str(project_root), capture_output=True, text=True, env=env
        )
        
        if result.returncode == 0:
            import base64
            
            # Find the most recently generated policyChecker folder
            checker_dirs = [d for d in export_dir.glob("policyChecker_*") if d.is_dir()]
            if not checker_dirs:
                return JSONResponse(
                    status_code=500,
                    content={'error': 'Validation framework succeeded but no policyChecker directory was generated.'}
                )
            
            # Sort by modification time, newest first
            checker_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            latest_checker_dir = checker_dirs[0]
            
            # Create a ZIP archive of the latest checker directory
            zip_path = latest_checker_dir.with_suffix('.zip')
            shutil.make_archive(str(latest_checker_dir), 'zip', str(latest_checker_dir))
            
            # Read the zip file and encode to base64
            with open(zip_path, "rb") as f:
                zip_data = f.read()
                zip_base64 = base64.b64encode(zip_data).decode('utf-8')
                
            # Read the generated validation logic to display on screen
            validation_job_path = latest_checker_dir / 'validation_job.py'
            validation_code = ""
            if validation_job_path.exists():
                with open(validation_job_path, "r") as f:
                    validation_code = f.read()

            return {
                'message': 'Validation service successfully generated.',
                'export_dir': str(latest_checker_dir),
                'zip_base64': zip_base64,
                'zip_filename': f"{latest_checker_dir.name}.zip",
                'validation_code': validation_code,
                'logs': result.stdout
            }
        else:
            logging.error(f"Framework execution failed: {result.stderr or result.stdout}")
            return JSONResponse(
                status_code=500,
                content={
                    'error': 'Validation framework failed to generate service.',
                    'logs': result.stderr or result.stdout
                }
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error generating validation service: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if 'backup_path' in locals() and backup_path.exists():
            shutil.move(str(backup_path), str(dp1_path))
        if 'sdm_backup_path' in locals() and sdm_backup_path.exists():
            shutil.move(str(sdm_backup_path), str(sdm_path))

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5000)