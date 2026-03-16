FROM python:3.12-slim

WORKDIR /app

# Install git (needed for pip git+ installs from GitHub)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Build metadata (set by CI/CD via --build-arg)
ARG VERSION=dev
ARG BUILD_DATE=unknown
ARG GIT_COMMIT=unknown
ENV APP_VERSION=${VERSION} \
    APP_BUILD_DATE=${BUILD_DATE} \
    APP_GIT_COMMIT=${GIT_COMMIT}

# Copy source code
COPY lib/ lib/
COPY scripts/ scripts/
COPY web/ web/

# Make entrypoint executable
RUN chmod +x scripts/entrypoint.sh

# Create data directory
RUN mkdir -p data

# Dashboard port
EXPOSE 8005

# Entrypoint runs both bot + dashboard
ENTRYPOINT ["scripts/entrypoint.sh"]
