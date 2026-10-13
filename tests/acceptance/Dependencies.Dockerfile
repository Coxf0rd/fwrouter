ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG DEBIAN_SNAPSHOT
ARG DEPENDENCY_LOCK_SHA256
LABEL org.opencontainers.image.base.name="${BASE_IMAGE}"
LABEL io.fwrouter.acceptance.dependencies.lock-sha256="${DEPENDENCY_LOCK_SHA256}"
WORKDIR /opt/fwrouter-test
COPY dependency-image.lock.json /opt/fwrouter-test/dependency-image.lock.json
COPY requirements-ci.txt /tmp/requirements-ci.txt
COPY requirements-browser.txt /tmp/requirements-browser.txt
COPY native/xray /opt/fwrouter-test/bin/xray
COPY native/mihomo /opt/fwrouter-test/bin/mihomo
COPY native/chromium.tar /tmp/fwrouter-chromium.tar
RUN python -c 'import hashlib,json,os,pathlib,sys; p=pathlib.Path("/opt/fwrouter-test/dependency-image.lock.json"); l=json.loads(p.read_text()); d=hashlib.sha256(json.dumps(l,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(); assert l["base_image"]==sys.argv[1] and l["debian_snapshot"]["url"]==sys.argv[2] and d==os.environ["DEPENDENCY_LOCK_SHA256"]' "${BASE_IMAGE}" "${DEBIAN_SNAPSHOT}" \
    && python -c 'import json,pathlib; l=json.loads(pathlib.Path("/opt/fwrouter-test/dependency-image.lock.json").read_text()); print("deb [check-valid-until=no] "+l["debian_snapshot"]["url"]+" "+l["debian_snapshot"]["suite"]+" "+" ".join(l["debian_snapshot"]["components"]))' > /tmp/fwrouter-snapshot.list \
    && python -c 'import json,pathlib; l=json.loads(pathlib.Path("/opt/fwrouter-test/dependency-image.lock.json").read_text()); print(" ".join(l["apt_packages"]))' > /tmp/fwrouter-apt-packages.list \
    && apt-get -o APT::Get::AllowUnauthenticated=false -o Acquire::AllowInsecureRepositories=false -o Acquire::AllowDowngradeToInsecureRepositories=false -o Dir::Etc::sourcelist=/tmp/fwrouter-snapshot.list -o Dir::Etc::sourceparts=- -o Acquire::Check-Valid-Until=false update \
    && apt-get -o APT::Get::AllowUnauthenticated=false -o Acquire::AllowInsecureRepositories=false -o Acquire::AllowDowngradeToInsecureRepositories=false -o Dir::Etc::sourcelist=/tmp/fwrouter-snapshot.list -o Dir::Etc::sourceparts=- -o Acquire::Check-Valid-Until=false install --no-install-recommends -y $(cat /tmp/fwrouter-apt-packages.list) \
    && rm -rf /var/lib/apt/lists/* /tmp/fwrouter-snapshot.list /tmp/fwrouter-apt-packages.list \
    && python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes -r /tmp/requirements-ci.txt -r /tmp/requirements-browser.txt \
    && chmod 0755 /opt/fwrouter-test/bin/xray /opt/fwrouter-test/bin/mihomo \
    && mkdir -p /opt/fwrouter-test/chromium \
    && tar -xf /tmp/fwrouter-chromium.tar -C /opt/fwrouter-test/chromium \
    && python -c 'import hashlib,json,pathlib; p=pathlib.Path("/opt/fwrouter-test/dependency-image.lock.json"); l=json.loads(p.read_text()); digest=lambda f: hashlib.sha256(pathlib.Path(f).read_bytes()).hexdigest(); a=l["assets"]; assert digest("/opt/fwrouter-test/bin/xray")==a["xray"]["binary_sha256"]; assert digest("/opt/fwrouter-test/bin/mihomo")==a["mihomo"]["binary_sha256"]; assert digest("/tmp/fwrouter-chromium.tar")==a["chromium"]["bundle_sha256"]; assert digest("/opt/fwrouter-test/chromium/chrome-linux64/chrome")==a["chromium"]["executable_sha256"]' \
    && rm -f /tmp/requirements-ci.txt /tmp/requirements-browser.txt /tmp/fwrouter-chromium.tar \
    && dpkg-query -W -f='${binary:Package}=${Version}\n' | LC_ALL=C sort > /opt/fwrouter-test/dpkg-packages.txt \
    && python -c 'import hashlib,json,os,pathlib,subprocess; p=pathlib.Path("/opt/fwrouter-test/dependency-image.lock.json"); l=json.loads(p.read_text()); lockhash=hashlib.sha256(json.dumps(l,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest(); packages=pathlib.Path("/opt/fwrouter-test/dpkg-packages.txt").read_text().splitlines(); m={"schema":"fwrouter-acceptance-dependency-image/v1","lock_sha256":lockhash,"base_image":l["base_image"],"platform":"linux/"+subprocess.check_output(["dpkg","--print-architecture"],text=True).strip(),"debian_snapshot":l["debian_snapshot"],"installed_packages":packages,"requirements":l["requirements"],"assets":l["assets"],"playwright_version":l["playwright_version"]}; pathlib.Path("/opt/fwrouter-test/dependency-manifest.json").write_text(json.dumps(m,sort_keys=True,separators=(",",":"))+"\n"); assert m["platform"]==l["platform"] and lockhash==os.environ["DEPENDENCY_LOCK_SHA256"]'
ENV PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
