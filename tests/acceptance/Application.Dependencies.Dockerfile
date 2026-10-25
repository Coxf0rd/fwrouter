ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
ARG ACCEPTANCE_RUN_ID
ARG SOURCE_REVISION
LABEL io.fwrouter.acceptance.owner="fwrouter-test-harness-v1"
LABEL io.fwrouter.acceptance.run="${ACCEPTANCE_RUN_ID}"
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
LABEL io.fwrouter.acceptance.dependencies.image="${BASE_IMAGE}"

WORKDIR /workspace
COPY tests/acceptance/dependency-image.lock.json /tmp/fwrouter-dependency/dependency-image.lock.json
COPY tests/acceptance/dependency-image-consumer.lock.json /tmp/fwrouter-dependency/dependency-image-consumer.lock.json
COPY tests/acceptance/verify_dependency_image.py /tmp/fwrouter-dependency/verify_dependency_image.py
COPY .fwrouter-acceptance-revision /workspace/.fwrouter-acceptance-revision
COPY backend/ /workspace/backend/
COPY backend/tests/ /workspace/backend/tests/
COPY ui/ /workspace/ui/
COPY tests/application_acceptance/ /workspace/tests/application_acceptance/
COPY tests/acceptance/ /workspace/tests/acceptance/
COPY tests/gates/requirements-ci.txt /workspace/tests/gates/requirements-ci.txt
COPY host/libexec/fwrouter/traffic-collect.sh /workspace/host/libexec/fwrouter/traffic-collect.sh
COPY host/libexec/fwrouter/dataplane-common.sh /workspace/host/libexec/fwrouter/dataplane-common.sh
COPY host/libexec/fwrouter/dataplane-check.sh /workspace/host/libexec/fwrouter/dataplane-check.sh
COPY host/libexec/fwrouter/dataplane-apply.sh /workspace/host/libexec/fwrouter/dataplane-apply.sh
COPY host/libexec/fwrouter/dataplane-rollback.sh /workspace/host/libexec/fwrouter/dataplane-rollback.sh
COPY host/libexec/fwrouter/dataplane-snapshot-state.py /workspace/host/libexec/fwrouter/dataplane-snapshot-state.py
COPY --chmod=0755 host/libexec/fwrouter/dataplane-common.sh /usr/local/libexec/fwrouter/dataplane-common.sh
COPY --chmod=0755 host/libexec/fwrouter/dataplane-check.sh /usr/local/libexec/fwrouter/dataplane-check.sh
COPY --chmod=0755 host/libexec/fwrouter/dataplane-apply.sh /usr/local/libexec/fwrouter/dataplane-apply.sh
COPY --chmod=0755 host/libexec/fwrouter/dataplane-rollback.sh /usr/local/libexec/fwrouter/dataplane-rollback.sh
COPY --chmod=0755 host/libexec/fwrouter/dataplane-snapshot-state.py /usr/local/libexec/fwrouter/dataplane-snapshot-state.py
RUN python /tmp/fwrouter-dependency/verify_dependency_image.py \
      --base-image "${BASE_IMAGE}" --source-revision "${SOURCE_REVISION}"

ENV PYTHONPATH=/workspace/backend
ENV FWROUTER_XRAY_BINARY=/opt/fwrouter-test/bin/xray
ENV FWROUTER_MIHOMO_BINARY=/opt/fwrouter-test/bin/mihomo
ENV FWROUTER_BROWSER_EXECUTABLE=/opt/fwrouter-test/chromium/chrome-linux64/chrome
ENV FWROUTER_CHROMIUM_BINARY=/opt/fwrouter-test/chromium/chrome-linux64/chrome
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/fwrouter-test/playwright-browsers
USER 10001:10001
ENTRYPOINT ["python"]
