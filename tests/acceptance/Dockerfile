ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG ACCEPTANCE_RUN_ID
ARG SOURCE_REVISION
LABEL io.fwrouter.acceptance.owner="fwrouter-test-harness-v1"
LABEL io.fwrouter.acceptance.run="${ACCEPTANCE_RUN_ID}"
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
WORKDIR /workspace
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
       fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 \
       libcairo2 libcups2 libdbus-1-3 libdrm2 libfontconfig1 libgbm1 libglib2.0-0 \
       libnspr4 libnss3 libpango-1.0-0 libx11-6 libx11-xcb1 libxcb1 libxcb-dri3-0 \
       libxcb-render0 libxcb-shm0 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
       libxkbcommon0 libxrandr2 libxshmfence1 libxss1 libxtst6 \
    && rm -rf /var/lib/apt/lists/*
COPY backend/ /workspace/backend/
COPY backend/tests/ /workspace/backend/tests/
COPY ui/ /workspace/ui/
COPY tests/application_acceptance/ /workspace/tests/application_acceptance/
COPY tests/acceptance/ /workspace/tests/acceptance/
COPY tests/gates/requirements-ci.txt /workspace/tests/gates/requirements-ci.txt
COPY host/libexec/fwrouter/traffic-collect.sh /workspace/host/libexec/fwrouter/traffic-collect.sh
COPY .fwrouter-acceptance-revision /workspace/.fwrouter-acceptance-revision
COPY native/xray /opt/fwrouter-test/bin/xray
COPY native/mihomo /opt/fwrouter-test/bin/mihomo
COPY native/chromium/ /opt/fwrouter-test/chromium/
RUN python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes \
    -r /workspace/tests/gates/requirements-ci.txt \
    -r /workspace/tests/acceptance/requirements-browser.txt
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/workspace/backend
ENV FWROUTER_XRAY_BINARY=/opt/fwrouter-test/bin/xray
ENV FWROUTER_MIHOMO_BINARY=/opt/fwrouter-test/bin/mihomo
ENV FWROUTER_BROWSER_EXECUTABLE=/opt/fwrouter-test/chromium/chrome-linux64/chrome
ENV FWROUTER_CHROMIUM_BINARY=/opt/fwrouter-test/chromium/chrome-linux64/chrome
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/fwrouter-test/playwright-browsers
USER 10001:10001
ENTRYPOINT ["python"]
