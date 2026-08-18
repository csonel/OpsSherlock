# OpsSherlock runtime image.
FROM python:3.14-slim

ARG KUBECTL_VERSION=v1.30.2
ARG HELM_VERSION=v3.15.2

# kubectl + helm
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
    case "$(dpkg --print-architecture)" in \
        arm64) ARCH=arm64 ;; \
        amd64) ARCH=amd64 ;; \
        *) echo "unsupported arch"; exit 1 ;; \
    esac; \
    curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" -o /usr/local/bin/kubectl; \
    chmod +x /usr/local/bin/kubectl; \
    curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" | tar -xz -C /tmp; \
    mv "/tmp/linux-${ARCH}/helm" /usr/local/bin/helm; \
    chmod +x /usr/local/bin/helm; \
    rm -rf /tmp/linux-${ARCH}; \
    apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "agent.py"]
