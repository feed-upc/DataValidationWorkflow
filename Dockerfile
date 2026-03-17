FROM python:3.12-slim

# Install system dependencies (Node.js for EYE reasoner, Docker CLI for building PolicyCheckers)
RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    ca-certificates \
    curl \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install EYE reasoner (Node.js)
RUN npm init -y && npm install eyereasoner

# Install Python requirements
COPY api_requirements.txt .
RUN pip install --no-cache-dir -r api_requirements.txt

# Copy the ValidationFramework source
COPY . /app/

# PROJECT_ROOT: mount the full project here at runtime for demo/run_demo.py, SDM, data access
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
