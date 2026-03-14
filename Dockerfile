FROM python:3.12-slim

# Install system dependencies (Node.js for EYE reasoner)
RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install EYE reasoner (Node.js)
RUN npm init -y && npm install eyereasoner

# Install Python requirements
COPY api/api_requirements.txt .
RUN pip install --no-cache-dir -r api_requirements.txt

# Copy the ValidationFramework source
COPY . /app/

# PROJECT_ROOT: mount the full project here for demo/run_demo.py access
# SDM_PATH: path to the Semantic Data Model (sdm.ttl)
# Both can be overridden at runtime via -e or docker-compose
ENV PYTHONPATH=/app
ENV PATH="/app/node_modules/.bin:${PATH}"
ENV PROJECT_ROOT=/project
ENV SDM_PATH=/project/FederatedComputationalGovernance/SemanticDataModel/sdm.ttl

EXPOSE 5000

# Usage:
#   docker build -t validation-framework-api .
#   docker run -p 5000:5000 -v /path/to/Fdatavalidation-1:/project validation-framework-api
CMD ["python", "api/api.py"]
