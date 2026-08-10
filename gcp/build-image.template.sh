#!/usr/bin/env bash

# Helper script to build GCE VM images for GitHub Actions Runners from startup script

#shellcheck disable=SC2154

set -e

TEMP_VM_NAME="${image_name}-builder-$(date +%s)"
DISK_NAME="ssd-${image_name}-builder-$(date +%s)"

# How long to wait for install.sh to finish before giving up. The bake is
# ~7-10 minutes; this is a backstop against waiting forever on a VM whose
# startup script wedged (the previous `while true` loop had no exit).
BAKE_TIMEOUT_SECONDS="${bake_timeout_seconds}"

echo "Building VM image: ${image_name}"
echo "Project ID: ${project_id}"
echo "Startup script: ${startup_script_gcs}"
echo "Machine type: ${machine_type}"
echo "Zone: ${zone}"
echo "Temporary VM: $TEMP_VM_NAME"
echo ""

# Delete the builder VM on any failure. Without this an aborted bake leaves a
# STANDARD (non-Spot) VM running and billing indefinitely.
cleanup_failed_builder() {
	echo "Cleaning up failed builder VM $TEMP_VM_NAME..." >&2
	gcloud compute instances delete "$TEMP_VM_NAME" \
		--project="${project_id}" --zone="${zone}" --quiet >/dev/null 2>&1 || true
}

# Step 1: Create GCE VM with startup script from GCS
echo "[1/5] Creating temporary VM instance..."
gcloud compute instances create "$TEMP_VM_NAME" \
	--project="${project_id}" \
	--zone="${zone}" \
	--machine-type="${machine_type}" \
	--network-interface="stack-type=IPV4_ONLY,subnet=${subnet},no-address" \
	--metadata="enable-oslogin=true,enable-guest-attributes=TRUE,startup-script-url=${startup_script_gcs}" \
	--maintenance-policy="MIGRATE" \
	--provisioning-model="STANDARD" \
	--service-account="${service_account}" \
	--scopes="https://www.googleapis.com/auth/cloud-platform" \
	--create-disk="auto-delete=yes,boot=yes,name=$DISK_NAME,image=${image_family},mode=rw,type=${disk_type},size=${disk_size},provisioned-iops=${disk_provisioned_iops},provisioned-throughput=${disk_provisioned_throughput}" \
	--no-shielded-secure-boot \
	--shielded-vtpm \
	--shielded-integrity-monitoring \
	--reservation-affinity=any \
	--quiet

echo "VM instance created: $TEMP_VM_NAME"
echo ""

# Step 2: Wait until VM is terminated (startup script completes and shuts down)
echo "[2/5] Waiting for VM to terminate (startup script execution)..."
echo "This may take several minutes depending on the startup script..."

WAITED=0
while true; do
	STATUS=$(gcloud compute instances describe "$TEMP_VM_NAME" \
		--project="${project_id}" \
		--zone="${zone}" \
		--format="get(status)" --quiet 2>/dev/null || echo "NOTFOUND")

	if [ "$STATUS" = "TERMINATED" ]; then
		echo "VM has stopped"
		break
	fi

	if [ "$WAITED" -ge "$BAKE_TIMEOUT_SECONDS" ]; then
		echo "ERROR: VM did not stop within $${BAKE_TIMEOUT_SECONDS}s (status: $STATUS)." >&2
		echo "install.sh has probably wedged. Serial console output:" >&2
		gcloud compute instances get-serial-port-output "$TEMP_VM_NAME" \
			--project="${project_id}" --zone="${zone}" 2>/dev/null | tail -50 >&2 || true
		cleanup_failed_builder
		exit 1
	fi

	echo "Current status: $STATUS - waiting 10 seconds..."
	sleep 10
	WAITED=$((WAITED + 10))
done

echo ""

# Step 3: Verify the bake actually succeeded BEFORE promoting the disk
#
# install.sh shuts the VM down via an EXIT trap on success AND on failure, so
# "TERMINATED" says nothing about whether the bake worked — and GCE discards
# serial output once an instance stops, so there is nothing left to grep. Skip
# this check and a failed bake silently publishes a broken image into the
# family every runner VM boots from.
#
# install.sh writes this guest attribute as its very last action, so it exists
# only if every preceding step succeeded.
echo "[3/5] Verifying bake status..."
BAKE_STATUS=$(gcloud compute instances get-guest-attributes "$TEMP_VM_NAME" \
	--project="${project_id}" \
	--zone="${zone}" \
	--query-path="develo/bake-status" \
	--format="value(value)" --quiet 2>/dev/null || echo "")

if [ -z "$BAKE_STATUS" ]; then
	echo "ERROR: no develo/bake-status guest attribute on $TEMP_VM_NAME." >&2
	echo "install.sh did not reach its final step, so the bake FAILED. Refusing" >&2
	echo "to publish a broken image to family '${image_name}'." >&2
	echo "The builder VM is left in place for inspection; delete it when done:" >&2
	echo "  gcloud compute instances delete $TEMP_VM_NAME --zone=${zone} --project=${project_id}" >&2
	exit 1
fi

echo "Bake status: $BAKE_STATUS"
BAKED_RUNNER_VERSION=$(echo "$BAKE_STATUS" | sed -n 's/.*runner_version=\([^ ]*\).*/\1/p')
echo "Baked actions/runner version: $${BAKED_RUNNER_VERSION:-unknown}"
echo ""

# Step 4: Create disk image from the VM's boot disk
echo "[4/5] Creating disk image from VM boot disk..."
gcloud compute images create "${image_name}-v$(date -u +%Y-%m-%d)-$(date +%s)" \
	--project="${project_id}" \
	--source-disk="$DISK_NAME" \
	--source-disk-zone="${zone}" \
	--family="${image_name}" \
	--description="GitHub Actions runner image built from ${startup_script_gcs} on $(date -u +%Y-%m-%dT%H:%M:%SZ), actions/runner $${BAKED_RUNNER_VERSION:-unknown}" \
	--storage-location="${region}" \
	--labels="runner-version=$(echo "$${BAKED_RUNNER_VERSION:-unknown}" | tr '.' '-')" \
	--quiet

echo "Disk image created: ${image_name}"
echo ""

# Step 5: Delete the temporary VM
echo "[5/5] Cleaning up temporary VM..."
gcloud compute instances delete "$TEMP_VM_NAME" \
	--project="${project_id}" \
	--zone="${zone}" \
	--quiet

echo "Temporary VM deleted"
echo ""
echo "✓ Image build complete successfully: ${image_name} (actions/runner $${BAKED_RUNNER_VERSION:-unknown})"
