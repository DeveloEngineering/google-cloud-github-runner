#!/usr/bin/env bash

# Copyright 2025 Nils Knieling. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Install Docker and GitHub Actions Runner for Linux with x64 or ARM64 CPU architecture
# https://github.com/actions/runner
# https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners#linux
# https://docs.docker.com/engine/install/ubuntu/

# Exit on error, undefined variables, and pipe failures
set -euo pipefail

# ─── ALWAYS shut down on script exit, success or failure ─────────────────────
# Without this, a `set -e` exit anywhere below leaves the VM in state
# RUNNING forever — terraform's build-image-*.sh wait loop spins
# indefinitely. The trap guarantees the VM terminates so terraform can move
# on (or fail-fast, depending on what got baked in before the failure).
trap '
        exit_code=$?
        echo "─── install.sh exiting with code ${exit_code} — shutting down VM in 10s ───"
        sleep 10
        sudo shutdown -h now || true
' EXIT

# Set default GitHub Actions Runner installation directory
readonly MY_RUNNER_DIR="/actions-runner"

# Prevent interactive prompts during package installation
export DEBIAN_FRONTEND=noninteractive

# Corepack >= 0.30 prompts to confirm package downloads. CI has no TTY, so
# any prompt would hang. Disable it on this VM image, both for the current
# install.sh run and for all future logins (actions runner job context
# included).
echo 'COREPACK_ENABLE_DOWNLOAD_PROMPT=0' | sudo tee -a /etc/environment >/dev/null
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0

# Tell actions/setup-node@v4 where the pre-installed Node tool cache lives.
# Without this env var (GitHub-hosted runners set it automatically; self-
# hosted runners do not), setup-node falls back to downloading Node fresh
# on every job — wasting ~10s and defeating the pre-bake entirely.
echo 'RUNNER_TOOL_CACHE=/opt/hostedtoolcache' | sudo tee -a /etc/environment >/dev/null
export RUNNER_TOOL_CACHE=/opt/hostedtoolcache

# ─── develo CI customizations: versioned constants ───────────────────────────
# Keep these aligned with the consuming repo:
#   NODE_VERSION       ↔ node-version: in develo-emr/.github/workflows/ci.yml
#   PLAYWRIGHT_VERSION ↔ @playwright/test in develo-emr/e2e/package.json
#   YARN_VERSION       ↔ packageManager in develo-emr/package.json
# Mismatch is non-fatal: CI falls back to download-at-runtime.
readonly DEVELO_NODE_VERSION="24.13.0"
readonly DEVELO_PLAYWRIGHT_VERSION="1.58.2"
readonly DEVELO_YARN_VERSION="4.14.1"
readonly DEVELO_AR_PROJECT="gh-runners-496913"
readonly DEVELO_AR_LOCATION="us-central1"
readonly DEVELO_AR_REPO="ci-image-mirror"
readonly DEVELO_AR_REGISTRY="${DEVELO_AR_LOCATION}-docker.pkg.dev"

# Function to exit the script with a failure message
exit_with_failure() {
        echo >&2 "FAILURE: $1"
        exit 1
}

# Detect CPU architecture early
case $(uname -m) in
        aarch64|arm64)
                readonly MY_ARCH="arm64"
                ;;
        amd64|x86_64)
                readonly MY_ARCH="x64"
                ;;
        *)
                exit_with_failure "Cannot determine CPU architecture!"
                ;;
esac

# ─── develo CI customizations: kill Ubuntu auto-update machinery ─────────────
# On fresh Ubuntu cloud-image boot, unattended-upgrades and
# update-notifier-download race for the dpkg lock and can deadlock. Don't
# try to wait them out; stop the services outright, kill any lingering
# processes, then bounded-wait for the lock to release. Then disable + mask
# so they can't restart.
echo "Stopping and masking Ubuntu auto-update services..."
sudo systemctl stop \
        unattended-upgrades.service \
        apt-daily.service \
        apt-daily-upgrade.service \
        update-notifier-download.service \
        2>/dev/null || true
sudo systemctl disable --now \
        unattended-upgrades.service \
        apt-daily.timer apt-daily.service \
        apt-daily-upgrade.timer apt-daily-upgrade.service \
        update-notifier-download.timer update-notifier-download.service \
        2>/dev/null || true
sudo systemctl mask \
        unattended-upgrades.service \
        apt-daily.service \
        apt-daily-upgrade.service \
        update-notifier-download.service \
        2>/dev/null || true

# Kill any lingering upgrader / apt processes that may hold locks.
sudo pkill -9 -f 'unattended-upgr|apt.systemd.daily|^/usr/bin/apt-get' 2>/dev/null || true
sleep 2

# Bounded wait (max 60s) for the dpkg lock to free up. Should be instant
# after the pkill above; defensive in case anything else is mid-transaction.
for _ in $(seq 1 30); do
        if ! sudo fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock >/dev/null 2>&1; then
                echo "  dpkg locks free"
                break
        fi
        echo "  dpkg locks still held — waiting..."
        sleep 2
done

# Recover from any half-finished dpkg transaction left by the killed upgrader.
sudo dpkg --configure -a 2>/dev/null || true

# Neuter the apt-periodic config so unattended-upgrades stays off even if
# something tries to re-enable it.
sudo tee /etc/apt/apt.conf.d/99disable-unattended-upgrades >/dev/null <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
EOF

# Install dependencies
# NB: awscli was removed from Ubuntu Noble's apt repositories. If you need
# the AWS CLI, install AWS CLI v2 via the official installer separately.
echo "Installing system dependencies..."
sudo apt-get update -yq
sudo apt-get install -y \
        apt-transport-https \
        apt-utils \
        build-essential \
        ca-certificates \
        curl \
        dnsutils \
        git \
        gpg \
        jq \
        lsb-release \
        netcat-openbsd \
        nodejs \
        npm \
        openssh-client \
        postgresql-client \
        python3-crcmod \
        python3-openssl \
        python3-pip \
        python3-venv \
        software-properties-common \
        tar \
        unzip \
        zip \
        zstd

# Verify required commands are available
readonly REQUIRED_COMMANDS=(curl gzip jq sed tar)
for cmd in "${REQUIRED_COMMANDS[@]}"; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
                exit_with_failure "Required command '$cmd' not found"
        fi
done

# Add Docker repository and install
echo "Installing Docker..."
sudo curl -fsSL "https://download.docker.com/linux/ubuntu/gpg" | sudo gpg --dearmor -o "/usr/share/keyrings/download.docker.com"
echo "deb [signed-by=/usr/share/keyrings/download.docker.com] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee "/etc/apt/sources.list.d/docker.list" >/dev/null
sudo apt-get update -yq
sudo apt-get install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin

# Enable and start Docker service
sudo systemctl enable docker.service
sudo systemctl start docker.service

# Create runner user and add to docker und sudoers group
echo "Creating runner user..."
if ! id -u runner >/dev/null 2>&1; then
        sudo useradd -m runner
fi
sudo usermod -aG docker,google-sudoers runner

# ─── Install GitHub Actions Runner ───────────────────────────────────────────
# Done EARLY (before any develo optimizations) so the image is always usable
# as a baseline runner image even if a later optional step fails. If this
# install fails, the EXIT trap shuts the VM down WITH non-zero exit_code, the
# image gets snapshotted broken, and runner VMs will fail at startup — but
# this is rarely flaky compared to npx/docker-pull steps below.
echo "Installing GitHub Actions Runner..."
MY_RUNNER_VERSION=$(curl -fsSL "https://api.github.com/repos/actions/runner/releases/latest" | jq -r '.tag_name' | sed 's/^v//')
if [[ -z "$MY_RUNNER_VERSION" || "$MY_RUNNER_VERSION" == "null" ]]; then
        exit_with_failure "Could not retrieve the latest GitHub Actions Runner version"
fi
echo "Installing GitHub Actions Runner version: v${MY_RUNNER_VERSION}"

sudo mkdir -p "$MY_RUNNER_DIR"
cd "$MY_RUNNER_DIR"
sudo curl -fsSL -O "https://github.com/actions/runner/releases/download/v${MY_RUNNER_VERSION}/actions-runner-linux-${MY_ARCH}-${MY_RUNNER_VERSION}.tar.gz"
sudo tar xzf "actions-runner-linux-${MY_ARCH}-${MY_RUNNER_VERSION}.tar.gz"

# Patch for Ubuntu 24.04 (https://github.com/actions/runner/issues/3150)
sudo sed -i 's/libicu72/libicu72 libicu74/' ./bin/installdependencies.sh

# Run the installation script
sudo ./bin/installdependencies.sh

# Sanity-check the runner is fully installed. If config.sh is missing here
# we want the bake to fail loudly, not produce a broken image silently.
[[ -x "$MY_RUNNER_DIR/config.sh" && -x "$MY_RUNNER_DIR/run.sh" ]] || \
        exit_with_failure "GitHub Actions Runner install failed: $MY_RUNNER_DIR/{config.sh,run.sh} missing"
echo "GitHub Actions Runner installed successfully"

# Marker file so we can grep for it on a baked image to confirm runner install
# made it in. Future operators (or a verification step in build-image.sh) can
# look for this file before promoting an image.
sudo install -m 0644 /dev/null /etc/develo-runner-image-ready
echo "runner_installed=1" | sudo tee -a /etc/develo-runner-image-ready >/dev/null

# Helper: run a develo optimization block with errors logged but NOT fatal.
# Every block below is OPTIONAL — failures should leave a degraded-but-working
# baseline runner image, never a broken one missing /actions-runner.
develo_optional() {
        local label="$1"; shift
        echo "─── develo optional: ${label} ───"
        if ! ( set -euo pipefail; "$@" ); then
                echo "  WARN: develo optional step '${label}' failed; continuing with degraded image" >&2
        fi
}

# ─── develo CI customizations: pre-install Node 24 in tool cache ─────────────
# actions/setup-node@v4 looks for Node at $RUNNER_TOOL_CACHE/node/<ver>/<arch>.
# Pre-populating it turns the setup-node step from a ~10s download into a ~1s
# cache hit. DEVELO_NODE_ARCH is the Node release naming (x64/arm64), NOT
# the dpkg architecture name.
DEVELO_NODE_ARCH=$([ "${MY_ARCH}" = "arm64" ] && echo "arm64" || echo "x64")
DEVELO_TOOL_CACHE="/opt/hostedtoolcache"
DEVELO_NODE_DIR="${DEVELO_TOOL_CACHE}/node/${DEVELO_NODE_VERSION}/${DEVELO_NODE_ARCH}"

develo_install_node() {
        sudo mkdir -p "${DEVELO_NODE_DIR}"
        curl -fsSL "https://nodejs.org/dist/v${DEVELO_NODE_VERSION}/node-v${DEVELO_NODE_VERSION}-linux-${DEVELO_NODE_ARCH}.tar.xz" \
                | sudo tar -xJ --strip-components=1 -C "${DEVELO_NODE_DIR}"
        # Marker file that actions/setup-node checks for cache validity
        sudo touch "${DEVELO_TOOL_CACHE}/node/${DEVELO_NODE_VERSION}/${DEVELO_NODE_ARCH}.complete"
        # System-wide PATH for any non-setup-node consumers
        sudo ln -sf "${DEVELO_NODE_DIR}/bin/node"     /usr/local/bin/node
        sudo ln -sf "${DEVELO_NODE_DIR}/bin/npm"      /usr/local/bin/npm
        sudo ln -sf "${DEVELO_NODE_DIR}/bin/npx"      /usr/local/bin/npx
        sudo ln -sf "${DEVELO_NODE_DIR}/bin/corepack" /usr/local/bin/corepack
}
develo_optional "pre-install Node ${DEVELO_NODE_VERSION} in tool cache" develo_install_node

# Pre-download the exact yarn version develo-emr uses so the first CI
# `yarn ...` invocation doesn't fetch it. Combined with the
# COREPACK_ENABLE_DOWNLOAD_PROMPT=0 env var at the top, yarn is silent and
# instant on first use. Keep DEVELO_YARN_VERSION aligned with the
# packageManager field in develo-emr/package.json.
# `corepack enable yarn` writes the yarn shim to the directory containing
# the corepack binary it was invoked from — i.e. /usr/local/bin/yarn here,
# next to /usr/local/bin/corepack (the symlink we just created). That path
# is in the default PATH for both shell users and the actions-runner job
# environment, so CI's `which yarn` finds it.
#
# Do NOT add a fallback `ln -sf "${DEVELO_NODE_DIR}/bin/yarn" /usr/local/bin/yarn`
# here — that would overwrite the real shim with a broken symlink to a
# file corepack does NOT create in the Node bin dir.
develo_install_yarn() {
        sudo corepack enable yarn
        sudo COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack prepare "yarn@${DEVELO_YARN_VERSION}" --activate
        # Pre-warm the runner user's corepack cache so the first CI yarn invocation
        # doesn't re-download yarn 4.14.1 (cache is per-user; root's cache from
        # above doesn't help the runner user).
        sudo -u runner -E env \
                HOME="/home/runner" \
                PATH="/usr/local/bin:${PATH}" \
                COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
                corepack prepare "yarn@${DEVELO_YARN_VERSION}" --activate
        sudo chown -R runner:runner "${DEVELO_TOOL_CACHE}"
}
develo_optional "pre-install yarn ${DEVELO_YARN_VERSION} via corepack" develo_install_yarn

# ─── develo CI customizations: Playwright chromium runtime libs ──────────────
# Pre-installs the system deps `playwright install --with-deps chromium` would
# pull. With these baked in, CI can drop --with-deps. apt-mark hold prevents
# future apt-gets from swapping versions.
develo_install_playwright_libs() {
        local DEVELO_PLAYWRIGHT_LIBS=(
                libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64
                libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2
                libgbm1 libpango-1.0-0 libcairo2 libasound2t64
        )
        sudo apt-get install -y --no-install-recommends "${DEVELO_PLAYWRIGHT_LIBS[@]}"
        sudo apt-mark hold "${DEVELO_PLAYWRIGHT_LIBS[@]}" 2>/dev/null || true
}
develo_optional "Playwright chromium runtime libs" develo_install_playwright_libs

# ─── develo CI customizations: pre-install Playwright chromium browser ───────
# Cache the browser binary at the runner user's standard Playwright cache
# location so the CI `playwright install chromium` step is a no-op.
# Coupled to DEVELO_PLAYWRIGHT_VERSION — if e2e/package.json bumps
# @playwright/test, CI falls back to runtime download (same as today).
develo_install_playwright_browser() {
        sudo -u runner -E env \
                HOME="/home/runner" \
                PATH="/usr/local/bin:${PATH}" \
                bash -c "
                        set -euo pipefail
                        export PLAYWRIGHT_BROWSERS_PATH=\$HOME/.cache/ms-playwright
                        mkdir -p \"\$PLAYWRIGHT_BROWSERS_PATH\"
                        npx --yes playwright@${DEVELO_PLAYWRIGHT_VERSION} install chromium
                "
}
develo_optional "Playwright ${DEVELO_PLAYWRIGHT_VERSION} chromium browser" develo_install_playwright_browser

# ─── develo CI customizations: pre-pull CI service-container Docker images ───
# Saves ~30s per playwright shard. Requires the builder VM's service account
# (github-runners@gh-runners-496913.iam.gserviceaccount.com — the same SA
# the build-image-*.sh wrapper attaches to the VM) to have
# roles/artifactregistry.reader on ${DEVELO_AR_PROJECT}. Pull failures are
# logged but don't fail the build — runner VMs would just fall back to
# pulling at job time.
develo_prepull_images() {
        if ! command -v gcloud >/dev/null 2>&1; then
                echo "  WARN: gcloud not installed on builder VM; skipping AR image pre-pull"
                return 0
        fi
        sudo gcloud auth configure-docker "${DEVELO_AR_REGISTRY}" --quiet || \
                echo "  WARN: gcloud auth configure-docker failed; AR pulls may be skipped"
        local DEVELO_IMAGES=(
                "${DEVELO_AR_REGISTRY}/${DEVELO_AR_PROJECT}/${DEVELO_AR_REPO}/postgres:14"
                "${DEVELO_AR_REGISTRY}/${DEVELO_AR_PROJECT}/${DEVELO_AR_REPO}/redis:7"
                "public.ecr.aws/localstack/localstack:4.14.0"
                "${DEVELO_AR_REGISTRY}/${DEVELO_AR_PROJECT}/${DEVELO_AR_REPO}/hlnconsulting/ice:2.57.1"
                "${DEVELO_AR_REGISTRY}/${DEVELO_AR_PROJECT}/${DEVELO_AR_REPO}/medplum/medplum-server:3.3.0"
                "${DEVELO_AR_REGISTRY}/${DEVELO_AR_PROJECT}/${DEVELO_AR_REPO}/mockserver/mockserver:5.15.0"
                "${DEVELO_AR_REGISTRY}/${DEVELO_AR_PROJECT}/${DEVELO_AR_REPO}/atmoz/sftp:latest"
        )
        for IMG in "${DEVELO_IMAGES[@]}"; do
                echo "  pulling ${IMG}"
                sudo docker pull "${IMG}" || echo "  WARN: pull failed for ${IMG} (will fall back to runtime pull)"
        done
}
develo_optional "pre-pull CI service-container Docker images" develo_prepull_images

# Cleanup: Clear package cache and temporary files
echo "Cleaning up..."
sudo apt-get clean
sudo rm -rf /tmp/* /root/.cache
sudo rm -rf /var/lib/apt/lists/*

# Cleanup: Rotate and vacuum journal logs
sudo journalctl --rotate
sudo journalctl --vacuum-time=1s

# Cleanup: Remove compressed and rotated log files, then truncate remaining logs
sudo find /var/log -type f \( -name "*.gz" -o -regex ".*\.[0-9]$" \) -delete
sudo find /var/log -type f -exec truncate -s 0 {} +

echo "Setup completed successfully"

# Shutdown is handled by the EXIT trap at the top of this script — do not add
# `shutdown -h now` here, or it would race the trap.
