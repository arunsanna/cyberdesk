"""
cyberdesk_operator.py
---------------------
Kopf‑based Kubernetes operator that provisions and manages KubeVirt VMs for a custom
`Cyberdesk` CRD. Supabase is used as an external source of truth for instance state.

Key responsibilities
~~~~~~~~~~~~~~~~~~~~
* Bootstrap the operator (load configuration, check for golden snapshot).
* Handle `Cyberdesk` resource creation:
    * Check a `VirtualMachinePool` for an available, running "warm" VM.
    * If found, assign the VM from the pool (remove owner refs, add labels), patch its metadata, and notify the gateway.
    * If no warm VM is available, initiate a `VirtualMachineClone` from a golden `VirtualMachineSnapshot`.
    * Track provisioning state (pool vs. clone) via the `Cyberdesk` status.
* Keep Supabase in sync with KubeVirt `VirtualMachineInstance` phase changes for provisioned VMs.
* Handle `Cyberdesk` resource deletion or expiration:
    * Delete the associated `VirtualMachine`.
    * Attempt cleanup of any lingering `VirtualMachineClone` operations if provisioning didn't complete.

This single file keeps a clear top‑down structure:
    1. Standard‑library / third‑party imports
    2. Global configuration & logging
    3. Constants & enums
    4. Supabase and Kubernetes client bootstrap (sets gateway URL based on environment)
    5. Utility helpers (template loading, DB helpers, warm pool lookup, etc.)
    6. Kopf event‑handlers (startup, create/update/delete, timers, field watchers)

All helpers are deliberately *side‑effect free* (raise on error, return data), making
unit‑testing straightforward.
"""
from __future__ import annotations

import logging
import os
import socket
import urllib.request
import urllib.error
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

import kopf
import kubernetes
from dotenv import load_dotenv
from kopf import OperatorSettings
from kubernetes.client import (  # noqa: WPS433 — explicit import list for type checking
    CoreV1Api,
    CustomObjectsApi,
    ApiextensionsV1Api,
    ApiException,
)
from supabase import Client, create_client

# ---------------------------------------------------------------------------
# Logging & basic config -----------------------------------------------------
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Ensure ENV is loaded *early* so everything that relies on os.getenv works.
load_dotenv()

# ---------------------------------------------------------------------------
# Constants -----------------------------------------------------------------
# ---------------------------------------------------------------------------
CYBERDESK_GROUP = "cyberdesk.io"
CYBERDESK_VERSION = "v1alpha1"
CYBERDESK_PLURAL = "cyberdesks"
START_OPERATOR_PLURAL = "startcyberdeskoperators"

KUBEVIRT_GROUP = "kubevirt.io"
KUBEVIRT_VERSION = "v1"
KUBEVIRT_NAMESPACE = os.getenv("KUBEVIRT_NAMESPACE", "kubevirt")
KUBEVIRT_VM_PLURAL = "virtualmachines"
KUBEVIRT_VMI_PLURAL = "virtualmachineinstances"

MANAGED_BY = "cyberdesk-operator"
CYBERDESK_NAMESPACE = os.getenv("CYBERDESK_NAMESPACE", "cyberdesk-system")

# ---------------------------------------------------------------------------
# Enums ----------------------------------------------------------------------
# ---------------------------------------------------------------------------
class KubeVirtVMIPhase(str, Enum):
    """Supported phases as emitted by KubeVirt."""

    PENDING = "Pending"
    SCHEDULING = "Scheduling"
    SCHEDULED = "Scheduled"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    UNKNOWN = "Unknown"


class SupabaseInstanceStatus(str, Enum):
    """Canonical states stored in Supabase."""

    PENDING = "pending"
    RUNNING = "running"
    TERMINATED = "terminated"
    ERROR = "error"


# Static mapping between the two state machines -----------------------------
VMI_PHASE_TO_SUPABASE_STATUS: Dict[KubeVirtVMIPhase, SupabaseInstanceStatus] = {
    KubeVirtVMIPhase.PENDING: SupabaseInstanceStatus.PENDING,
    KubeVirtVMIPhase.SCHEDULING: SupabaseInstanceStatus.PENDING,
    KubeVirtVMIPhase.SCHEDULED: SupabaseInstanceStatus.PENDING,
    KubeVirtVMIPhase.RUNNING: SupabaseInstanceStatus.PENDING, # We now only denote running after cloud init is done
    KubeVirtVMIPhase.SUCCEEDED: SupabaseInstanceStatus.TERMINATED,
    KubeVirtVMIPhase.FAILED: SupabaseInstanceStatus.ERROR,
    KubeVirtVMIPhase.UNKNOWN: SupabaseInstanceStatus.ERROR,
}

CLONE_GROUP = "clone.kubevirt.io"
CLONE_VERSION = "v1beta1"
CLONE_PLURAL = "virtualmachineclones"
GOLDEN_SNAPSHOT_NAME = "snapshot-golden-vm" # Name of the golden snapshot source
SNAPSHOT_GROUP = "snapshot.kubevirt.io" # Correct API group for VirtualMachineSnapshot
SNAPSHOT_VERSION = "v1beta1" # Correct version for VirtualMachineSnapshot
SNAPSHOT_PLURAL = "virtualmachinesnapshots"

# ---------------------------------------------------------------------------
# Bootstrap helpers ----------------------------------------------------------
# ---------------------------------------------------------------------------

def _init_supabase() -> Client:
    """Create and return a Supabase client or raise ``kopf.PermanentError``."""
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        msg = "SUPABASE_URL / SUPABASE_KEY env vars must be set"
        logger.critical(msg)
        raise kopf.PermanentError(msg)

    try:
        client = create_client(supabase_url, supabase_key)
        logger.info("Supabase client initialised")
        return client
    except Exception as exc:  # noqa: BLE001 — log real error and abort
        logger.critical("Failed to initialise Supabase: %s", exc)
        raise kopf.PermanentError("Supabase init failed") from exc


def _init_kubernetes_clients() -> tuple[CoreV1Api, CustomObjectsApi, ApiextensionsV1Api]:
    """Return (core_v1, custom_objects, apiext) after loading config and set globals."""
    global IS_IN_CLUSTER, GATEWAY_BASE_URL # Declare modification intent
    try:
        kubernetes.config.load_kube_config()
        logger.info("Loaded kube‑config from local file")
        IS_IN_CLUSTER = False
        # Use a single env var for the full testing URL
        testing_url = os.getenv("GATEWAY_TESTING_URL")
        if testing_url:
            GATEWAY_BASE_URL = testing_url
            logger.info(f"Using configured local testing gateway URL: {GATEWAY_BASE_URL}")
            # Log a hint if it looks like the user might want host.docker.internal
            if "localhost" in testing_url:
                 logger.warning("GATEWAY_TESTING_URL contains 'localhost'. If operator and gateway run in separate local containers, consider using 'host.docker.internal' instead of 'localhost'.")
        else:
            logger.warning("Running locally but GATEWAY_TESTING_URL env var not set. Gateway notifications will be skipped.")
            GATEWAY_BASE_URL = None

    except kubernetes.config.config_exception.ConfigException:
        try:
            kubernetes.config.load_incluster_config()
            logger.info("Loaded in‑cluster kube‑config")
            IS_IN_CLUSTER = True
            GATEWAY_BASE_URL = "http://gateway.cyberdesk-system.svc.cluster.local:80"
            logger.info(f"Using in-cluster gateway URL: {GATEWAY_BASE_URL}")
        except kubernetes.config.config_exception.ConfigException as exc:
            logger.critical("Failed to load Kubernetes configuration: %s", exc)
            raise kopf.PermanentError("Cannot load Kubernetes config") from exc

    return CoreV1Api(), CustomObjectsApi(), ApiextensionsV1Api()


# Globals to store environment-dependent configuration set during init
IS_IN_CLUSTER = False # Default, will be updated by _init_kubernetes_clients
GATEWAY_BASE_URL: Optional[str] = None # Default, will be updated by _init_kubernetes_clients

# --- Bootstrap Clients ---
SUPABASE: Client = _init_supabase()
CORE_V1_API, CUSTOM_OBJECTS_API, APIEXT_V1_API = _init_kubernetes_clients()

# ---------------------------------------------------------------------------
# Supabase helpers -----------------------------------------------------------
# ---------------------------------------------------------------------------

def get_instance_status(instance_id: str) -> Optional[str]:
    """Return the current status for *instance_id* or ``None`` if missing/error."""
    try:
        logger.debug("Supabase query: status for %s", instance_id)
        resp = SUPABASE.table("cyberdesk_instances").select("status").eq("id", instance_id).limit(1).execute()
        return (resp.data[0]["status"] if resp.data else None)
    except Exception as exc:  # noqa: BLE001
        logger.error("Supabase error: %s", exc)
        return None


def update_instance_status(instance_id: str, vmi_phase: str) -> None:
    """Translate *vmi_phase* → Supabase status and update row if needed."""
    try:
        phase_enum = KubeVirtVMIPhase(vmi_phase)
    except ValueError:
        logger.error("Unknown VMI phase '%s' → marking ERROR", vmi_phase)
        target = SupabaseInstanceStatus.ERROR
    else:
        target = VMI_PHASE_TO_SUPABASE_STATUS.get(phase_enum, SupabaseInstanceStatus.ERROR)

    try:
        SUPABASE.table("cyberdesk_instances").update({"status": target.value}).eq("id", instance_id).execute()
        logger.info("Supabase status for %s set to %s", instance_id, target.value)
    except Exception as exc:  # noqa: BLE001
        logger.error("Supabase update failed for %s: %s", instance_id, exc)

# ---------------------------------------------------------------------------
# Kubernetes Helpers (including Warm Pool) -----------------------------------
# ---------------------------------------------------------------------------

def get_free_vm_from_pool(namespace: str, logger: kopf.Logger) -> Optional[str]:
    """
    Find, claim, and return the name of an available warm VM, or None.

    A VM is considered available if it has the 'pool.kubevirt.io/warm=ready'
    label, is Running, and does not have 'pool.kubevirt.io/in-use=true'.

    If found, the VM is "assigned" from the pool by:
    1. Removing ownerReferences.
    2. Setting 'pool.kubevirt.io/in-use=true' and 'pool.kubevirt.io/warm=claimed'.
    """
    pool_label_selector = "pool.kubevirt.io/warm=ready"
    logger.debug(f"Searching for warm VMs in namespace '{namespace}' with label '{pool_label_selector}'")
    try:
        vms = CUSTOM_OBJECTS_API.list_namespaced_custom_object(
            KUBEVIRT_GROUP,
            KUBEVIRT_VERSION,
            namespace,
            KUBEVIRT_VM_PLURAL,
            label_selector=pool_label_selector,
        )
    except ApiException as e:
        logger.error(f"Error listing VMs for warm pool: {e.status} {e.reason}")
        # Treat as temporary, maybe API server issue
        raise kopf.TemporaryError("Failed to list VMs for warm pool.", delay=15) from e

    for vm in vms.get("items", []):
        meta = vm.get("metadata", {})
        status = vm.get("status", {})
        labels = meta.get("labels", {})

        vm_name = meta.get("name")
        if not vm_name:
            logger.warning("Found VM in pool list without a name, skipping.")
            continue

        # Check if already marked as in-use by the pool logic itself
        if labels.get("pool.kubevirt.io/in-use") == "true":
            logger.debug(f"Warm VM '{vm_name}' found but already marked in-use, skipping.")
            continue

        # Check if running (important!)
        if status.get("printableStatus") != "Running":
            logger.debug(f"Warm VM '{vm_name}' found but not Running (status: {status.get('printableStatus')}), skipping.")
            continue

        # --- Assign the VM from Pool ---
        logger.info(f"Found available warm VM: '{vm_name}'. Attempting to assign from pool.")
        patch_body = {
            "metadata": {
                "ownerReferences": None,  # Detach from the pool controller
                "labels": {
                    **labels, # Keep existing labels
                    "pool.kubevirt.io/in-use": "true", # Mark as used
                    "pool.kubevirt.io/warm": "claimed", # Update pool status label
                    # Note: We will add cyberdesk-instance label in the main handler
                },
            }
        }
        try:
            CUSTOM_OBJECTS_API.patch_namespaced_custom_object(
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=namespace,
                plural=KUBEVIRT_VM_PLURAL,
                name=vm_name,
                body=patch_body,
            )
            logger.info(f"Successfully assigned warm VM '{vm_name}' from pool. Removed ownerReferences and added 'in-use' label.")
            return vm_name # Return the name of the assigned VM
        except ApiException as e:
            logger.error(f"Failed to patch (assign) warm VM '{vm_name}': {e.status} {e.reason}")
            # If patching fails, maybe the VM was deleted concurrently? Or permissions issue.
            # Log error and continue searching, maybe another VM will work.
            # If it's a transient issue, the next reconciliation might succeed.
            continue # Try the next VM in the list

    logger.info("No available warm VMs found in the pool.")
    return None

# ---------------------------------------------------------------------------
# CRD definition -------------------------------------------------------------
# ---------------------------------------------------------------------------
CYBERDESK_CRD_MANIFEST: dict = {
    "apiVersion": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "metadata": {"name": f"{CYBERDESK_PLURAL}.{CYBERDESK_GROUP}"},
    "spec": {
        "group": CYBERDESK_GROUP,
        "scope": "Namespaced",
        "names": {
            "plural": CYBERDESK_PLURAL,
            "singular": "cyberdesk",
            "kind": "Cyberdesk",
            "shortNames": ["cd", "cds"],
        },
        "versions": [
            {
                "name": CYBERDESK_VERSION,
                "served": True,
                "storage": True,
                "schema": {
                    "openAPIV3Schema": {
                        "type": "object",
                        "properties": {
                            "spec": {
                                "type": "object",
                                "properties": {
                                    "timeoutMs": {
                                        "type": "integer",
                                        "minimum": 1000,
                                        "description": "Milliseconds until VM is terminated.",
                                    }
                                },
                                "required": ["timeoutMs"],
                            },
                            "status": {
                                "type": "object",
                                "x-kubernetes-preserve-unknown-fields": True,
                            },
                        },
                    }
                },
                "subresources": {"status": {}},
            }
        ],
    },
}

# ---------------------------------------------------------------------------
# Kopf handlers --------------------------------------------------------------
# ---------------------------------------------------------------------------

def ensure_golden_snapshot_exists():
    """Check if the required golden VirtualMachineSnapshot exists."""
    logger.info(f"Checking for golden snapshot: {GOLDEN_SNAPSHOT_NAME} in {KUBEVIRT_NAMESPACE}")
    try:
        CUSTOM_OBJECTS_API.get_namespaced_custom_object(
            group=SNAPSHOT_GROUP,
            version=SNAPSHOT_VERSION,
            namespace=KUBEVIRT_NAMESPACE,
            plural=SNAPSHOT_PLURAL,
            name=GOLDEN_SNAPSHOT_NAME,
        )
        logger.info(f"Golden snapshot '{GOLDEN_SNAPSHOT_NAME}' found.")
    except ApiException as e:
        if e.status == 404:
            msg = f"Required golden snapshot '{GOLDEN_SNAPSHOT_NAME}' not found in namespace '{KUBEVIRT_NAMESPACE}'."
            logger.critical(msg)
            raise kopf.PermanentError(msg)
        else:
            msg = f"Error checking for golden snapshot '{GOLDEN_SNAPSHOT_NAME}': {e.status} {e.reason}"
            logger.error(msg)
            # Treat other errors as temporary to allow retries after potential cluster issues
            raise kopf.TemporaryError(msg, delay=30) from e
    except Exception as e:
        msg = f"Unexpected error checking for golden snapshot '{GOLDEN_SNAPSHOT_NAME}': {e}"
        logger.error(msg)
        raise kopf.TemporaryError(msg, delay=30) from e


@kopf.on.startup()
def configure_kopf(settings: OperatorSettings, **_: Dict[str, object]) -> None:
    """Tune watch timeouts and ensure golden snapshot exists."""
    settings.watching.server_timeout = 210  # seconds
    logger.info("Kopf watch server_timeout set to %s", settings.watching.server_timeout)
    # Check for snapshot on startup - operator won't function without it.
    ensure_golden_snapshot_exists()


@kopf.on.create(CYBERDESK_GROUP, CYBERDESK_VERSION, START_OPERATOR_PLURAL)
def crd_bootstrap(spec: dict, meta: dict, **_: Dict[str, object]) -> None:
    """Ensure the Cyberdesk CRD exists once the *bootstrap* resource is created."""
    try:
        APIEXT_V1_API.create_custom_resource_definition(body=CYBERDESK_CRD_MANIFEST)
        logger.info("Cyberdesk CRD applied")
    except kubernetes.client.rest.ApiException as exc:
        if exc.status == 409:  # already present
            logger.debug("Cyberdesk CRD already present")
        elif exc.status == 429:
            raise kopf.TemporaryError("API busy, retrying", delay=10) from exc
        else:
            raise kopf.PermanentError(f"CRD creation failed: {exc.status} {exc.reason}") from exc


def _ensure_vm_patched_and_running(vm_name: str, namespace: str, logger: kopf.Logger) -> None:
    """Fetch the VM and apply the required patches (metadata, spec, runStrategy)."""
    logger.info(f"Ensuring VM '{vm_name}' is patched and set to run.")
    # Labels intended for the VMI must go into spec.template.metadata.labels
    # Labels only relevant to the VM object itself can stay at the top level.
    patch_body = {
        "metadata": {
            "labels": {
                # Optional: Keep labels specific to the VM object itself here if needed.
                # For instance, if you wanted to label the VM resource differently than the VMI.
                "managed-by": MANAGED_BY, # Can be useful on the VM too
            }
            # Add top-level annotations for the VM if needed
        },
        "spec": {
            "runStrategy": "Always", # Ensure VM is set to run
            "template": {
                "metadata": { # <--- Ensure metadata exists here
                    "labels": { # <--- Labels for the VMI go here
                        "app": "cyberdesk",
                        "cyberdesk-instance": vm_name,
                        "managed-by": MANAGED_BY, # Also label the VMI for consistency
                        "kubevirt.io/domain": vm_name, # This is often set here
                    }
                    # Add annotations for the VMI if needed
                },
                "spec": {
                    "hostname": vm_name
                }
            }
        }
    }
    try:
        CUSTOM_OBJECTS_API.patch_namespaced_custom_object(
            group=KUBEVIRT_GROUP,
            version=KUBEVIRT_VERSION,
            namespace=namespace,
            plural=KUBEVIRT_VM_PLURAL,
            name=vm_name,
            body=patch_body
        )
        logger.info(f"Successfully patched VM '{vm_name}' metadata, spec, and runStrategy.")
    except ApiException as e:
        logger.error(f"Error patching VM '{vm_name}': {e.status} {e.reason}")
        # If patching fails, it's likely temporary or the VM was deleted.
        raise kopf.TemporaryError(f"Failed to patch VM {vm_name}", delay=10) from e


@kopf.on.create(CYBERDESK_GROUP, CYBERDESK_VERSION, CYBERDESK_PLURAL)
def cyberdesk_create(spec: dict, meta: dict, status: dict, logger: kopf.Logger, patch: kopf.Patch, body: dict, retry: int, **_: Dict[str, object]): # noqa: WPS211, WPS231
    """Reconcile a new Cyberdesk CR using status-driven warm pool/clone logic."""
    instance_id = meta["name"]
    namespace = KUBEVIRT_NAMESPACE
    timeout_ms = spec.get("timeoutMs", 3_600_000)
    max_clone_wait_retries = 20 # Used later in clone check
    clone_wait_delay = 5 # Increased delay slightly

    logger.info(f"Reconciling Cyberdesk CR '{instance_id}' (Attempt #{retry})")

    # --- Check Status: Determine current state/intent ---
    current_status = body.get("status", {}).get("cyberdesk_create", {})
    vm_ref = current_status.get("virtualMachineRef")
    clone_op_name = current_status.get("cloneOperationName")
    last_phase = current_status.get("lastPhase")

    # --- Idempotency Check: Already Provisioned? ---
    if vm_ref and last_phase in ["AssignedFromPool", "Cloned", "Running"]: # "Running" for older status compatibility
        logger.info(f"Cyberdesk '{instance_id}' already has vmRef '{vm_ref}'. Ensuring patch and returning.")
        try:
            # Make sure the VM (assigned or cloned) is correctly patched
            # _ensure_vm_patched_and_running should be idempotent
            _ensure_vm_patched_and_running(vm_ref, namespace, logger)

            # Ensure status reflects reality (especially startTime/expiryTime if they were missed)
            if "startTime" not in current_status or "expiryTime" not in current_status:
                 logger.warning(f"Status for {instance_id} with vmRef {vm_ref} is incomplete. Re-populating times.")
                 now = datetime.now(UTC)
                 expiry = now + timedelta(milliseconds=timeout_ms)
                 patch.status["cyberdesk_create"] = {
                      **current_status, # Keep existing fields like vmRef, lastPhase
                      "startTime": now.isoformat(),
                      "expiryTime": expiry.isoformat(),
                 }
            else:
                 # If status is complete, just ensure it's patched back (no-op if unchanged)
                 patch.status["cyberdesk_create"] = current_status

            # Clean up potential old top-level status fields
            if "virtualMachineRef" in patch.status: del patch.status["virtualMachineRef"]
            if "startTime" in patch.status: del patch.status["startTime"]
            if "expiryTime" in patch.status: del patch.status["expiryTime"]
            if "cloneOperationName" in patch.status.get("cyberdesk_create", {}):
                 del patch.status["cyberdesk_create"]["cloneOperationName"] # Should be removed if vmRef exists

        except kopf.TemporaryError:
             raise # Re-raise patch error
        except ApiException as e:
             if e.status == 404:
                  logger.warning(f"vmRef '{vm_ref}' in status for '{instance_id}' not found! Forcing re-provision.")
                  # Clear status to force reprovisioning
                  patch.status["cyberdesk_create"] = {}
             else:
                  logger.error(f"Error checking existing vmRef '{vm_ref}' for '{instance_id}': {e.reason}")
                  raise kopf.TemporaryError(f"Failed to check existing VM {vm_ref}", delay=10) from e
        else:
            return # AssignedFromPool successful

    # --- State Check: Already decided to clone? ---
    if clone_op_name:
        logger.info(f"Status indicates cloning operation '{clone_op_name}' already initiated for '{instance_id}'. Checking clone status.")
        # Skip warm pool check, go directly to checking the clone
        pass # Logic continues below in "Check Clone Status" section
    else:
        # --- State: Try Warm Pool ---
        logger.info(f"No active clone operation found in status for '{instance_id}'. Checking warm pool.")
        assigned_vm_name = get_free_vm_from_pool(namespace, logger) # Renamed variable for clarity

        if assigned_vm_name:
            logger.info(f"Using warm VM '{assigned_vm_name}' assigned from pool for Cyberdesk '{instance_id}'.")
            try:
                # --- Patch Assigned VM ---
                vm_patch_body = {
                    "metadata": {"labels": {"app": "cyberdesk", "cyberdesk-instance": instance_id, "managed-by": MANAGED_BY}},
                    "spec": {"template": {"metadata": {"labels": {"app": "cyberdesk", "cyberdesk-instance": instance_id, "managed-by": MANAGED_BY, "kubevirt.io/domain": instance_id}}}}
                }
                # Fetch current VM to merge labels correctly
                current_vm = CUSTOM_OBJECTS_API.get_namespaced_custom_object(KUBEVIRT_GROUP, KUBEVIRT_VERSION, namespace, KUBEVIRT_VM_PLURAL, assigned_vm_name)
                current_labels = current_vm.get("metadata", {}).get("labels", {})
                current_vmi_labels = current_vm.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})

                vm_patch_body["metadata"]["labels"] = {**current_labels, **vm_patch_body["metadata"]["labels"]}
                vm_patch_body["spec"]["template"]["metadata"]["labels"] = {**current_vmi_labels, **vm_patch_body["spec"]["template"]["metadata"]["labels"]}

                CUSTOM_OBJECTS_API.patch_namespaced_custom_object(KUBEVIRT_GROUP, KUBEVIRT_VERSION, namespace, KUBEVIRT_VM_PLURAL, assigned_vm_name, body=vm_patch_body)
                logger.info(f"Successfully patched VM '{assigned_vm_name}' assigned from pool for '{instance_id}'.")

                # --- Verify VMI is running and get IP (Readiness Check) ---
                try:
                    vmi = CUSTOM_OBJECTS_API.get_namespaced_custom_object(
                        group=KUBEVIRT_GROUP,
                        version=KUBEVIRT_VERSION,
                        namespace=namespace,
                        plural=KUBEVIRT_VMI_PLURAL,
                        name=assigned_vm_name # VMI name assumed to match assigned VM name
                    )
                    interfaces = vmi.get('status', {}).get('interfaces', [])
                    vmi_ip = interfaces[0].get('ipAddress') if interfaces else None
                    vmi_phase = vmi.get('status', {}).get('phase')

                    if not vmi_ip or vmi_phase != 'Running':
                         logger.warning(f"Pool-assigned VMI '{assigned_vm_name}' is not Running or has no IP yet (Phase: {vmi_phase}, IP: {vmi_ip}). Retrying.")
                         raise kopf.TemporaryError(f"Pool-assigned VMI {assigned_vm_name} not fully ready.")
                    logger.info(f"Verified pool-assigned VMI '{assigned_vm_name}' is Running with IP {vmi_ip}.")

                except ApiException as vmi_exc:
                    if vmi_exc.status == 404:
                         logger.warning(f"VMI '{assigned_vm_name}' not found immediately after patching VM. Retrying.")
                         raise kopf.TemporaryError("VMI not found yet after patching.") # Retry
                    else:
                         logger.error(f"API error getting VMI '{assigned_vm_name}' for readiness check: {vmi_exc.reason}")
                         raise # Re-raise other API errors

                # --- Notify Gateway ---
                if not GATEWAY_BASE_URL:
                    logger.warning(f"Gateway base URL not configured (In cluster? {IS_IN_CLUSTER}). Skipping notification for {instance_id}.")
                else:
                    gateway_url = f"{GATEWAY_BASE_URL}/cyberdesk/{instance_id}/ready"
                    logger.info(f"Notifying gateway for pool-assigned instance '{instance_id}' at {gateway_url}")
                    try:
                        req = urllib.request.Request(gateway_url, method="POST")
                        with urllib.request.urlopen(req, timeout=5) as response:
                            logger.info(f"Gateway notified successfully for pool-assigned '{instance_id}', status: {response.status}")
                    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                        logger.error(f"Failed to notify gateway for pool-assigned '{instance_id}': {e}")

            except ApiException as e:
                logger.error(f"Error patching/notifying for pool-assigned VM '{assigned_vm_name}': {e.reason}")
                raise kopf.TemporaryError(f"Failed to finalize pool-assigned VM {assigned_vm_name}", delay=10) from e

            # --- Update Status (AssignedFromPool Success) ---
            now = datetime.now(UTC)
            expiry = now + timedelta(milliseconds=timeout_ms)
            logger.info(f"Updating status for '{instance_id}': Assigned VM '{assigned_vm_name}' from pool, expires {expiry.isoformat()}")
            patch.status["cyberdesk_create"] = {
                "virtualMachineRef": assigned_vm_name,
                "startTime": now.isoformat(),
                "expiryTime": expiry.isoformat(),
                "lastPhase": "AssignedFromPool",
                # Explicitly remove cloneOperationName if it somehow existed
                "cloneOperationName": None,
            }
            # Clean up potential old status fields
            if "virtualMachineRef" in patch.status: del patch.status["virtualMachineRef"]
            if "startTime" in patch.status: del patch.status["startTime"]
            if "expiryTime" in patch.status: del patch.status["expiryTime"]
            return # Assignment from warm pool successful

        else:
            # --- State: Initiate Cloning ---
            logger.info(f"No warm VM available for '{instance_id}'. Initiating clone.")
            clone_op_name = f"clone-for-{instance_id}" # Define the clone op name

            # --- Update Status (Pre-Clone) ---
            # Set cloneOperationName *before* creating the clone object
            logger.info(f"Updating status for '{instance_id}': Setting cloneOperationName to '{clone_op_name}'")
            patch.status["cyberdesk_create"] = {
                "cloneOperationName": clone_op_name,
                "lastPhase": "CloningInitiated",
                # Ensure vmRef is not set here
                "virtualMachineRef": None,
            }
            # Return early to allow Kopf to patch the status.
            # The next reconciliation will pick up 'cloneOperationName' and proceed.
            # This prevents creating the Clone object if the status patch fails.
            raise kopf.TemporaryError(f"Clone operation name '{clone_op_name}' set in status. Retrying shortly to initiate/check clone.", delay=clone_wait_delay)

    # --- State: Check Clone Status (only reached if clone_op_name is set) ---
    if not clone_op_name:
         # This should ideally not be reached due to the logic structure, but acts as a safeguard.
         logger.error(f"Reached clone checking state for '{instance_id}' but cloneOperationName is not set in status. Retrying.")
         raise kopf.TemporaryError("Inconsistent state: clone check without cloneOperationName.", delay=10)

    logger.info(f"Checking status of clone operation '{clone_op_name}' for '{instance_id}'.")
    try:
        # --- Get or Create VirtualMachineClone Object ---
        # We try to *get* it first because the status might have been set on a previous attempt
        # where the actual clone creation failed afterwards.
        try:
            clone_obj = CUSTOM_OBJECTS_API.get_namespaced_custom_object(
                group=CLONE_GROUP, version=CLONE_VERSION, namespace=namespace, plural=CLONE_PLURAL, name=clone_op_name
            )
            logger.debug(f"Found existing VirtualMachineClone '{clone_op_name}'.")
        except ApiException as e:
            if e.status == 404:
                logger.info(f"VirtualMachineClone '{clone_op_name}' not found. Creating it now.")
                clone_body = {
                    "apiVersion": f"{CLONE_GROUP}/{CLONE_VERSION}",
                    "kind": "VirtualMachineClone",
                    "metadata": {"name": clone_op_name, "namespace": namespace, "labels": {"managed-by": MANAGED_BY, "cyberdesk-instance": instance_id}},
                    "spec": {
                        "source": {"apiGroup": SNAPSHOT_GROUP, "kind": "VirtualMachineSnapshot", "name": GOLDEN_SNAPSHOT_NAME},
                        "target": {
                             "apiGroup": KUBEVIRT_GROUP,
                             "kind": "VirtualMachine",
                             "name": instance_id,
                             "template": {
                                 "spec": {
                                     "readinessProbe": {
                                         "exec": {
                                              # Use test -f to check for cloud-init completion flag
                                             "command": ["test", "-f", "/var/lib/cloud/instance/boot-finished"]
                                         },
                                         "initialDelaySeconds": 30,
                                         "periodSeconds": 10,
                                         "failureThreshold": 3,
                                         "successThreshold": 1,
                                     }
                                 }
                             }
                        },
                    },
                }
                clone_obj = CUSTOM_OBJECTS_API.create_namespaced_custom_object(
                    group=CLONE_GROUP, version=CLONE_VERSION, namespace=namespace, plural=CLONE_PLURAL, body=clone_body
                )
                logger.info(f"VirtualMachineClone '{clone_op_name}' created.")
                # No need to check status immediately, let the next retry handle it
                raise kopf.TemporaryError(f"Clone {clone_op_name} just created. Waiting for status.", delay=clone_wait_delay)
            else:
                # Other API error getting the clone object
                logger.error(f"API error getting VirtualMachineClone '{clone_op_name}': {e.reason}")
                raise kopf.TemporaryError(f"Failed to get clone object {clone_op_name}", delay=clone_wait_delay) from e

        # --- Evaluate Clone Status ---
        current_clone_status = clone_obj.get("status", {})
        clone_phase = current_clone_status.get("phase")
        logger.info(f"Clone '{clone_op_name}' phase: {clone_phase}")

        if clone_phase == "Succeeded":
            logger.info(f"Clone '{clone_op_name}' succeeded. Finalizing VM '{instance_id}'.")
            # --- Ensure the newly created VM is patched and running ---
            _ensure_vm_patched_and_running(instance_id, namespace, logger) # Target VM name is instance_id

            # --- Update Status (Clone Success) ---
            now = datetime.now(UTC)
            expiry = now + timedelta(milliseconds=timeout_ms)
            logger.info(f"Updating status for '{instance_id}': Cloned VM '{instance_id}', expires {expiry.isoformat()}")
            patch.status["cyberdesk_create"] = {
                "virtualMachineRef": instance_id, # VM name matches instance_id
                "startTime": now.isoformat(),
                "expiryTime": expiry.isoformat(),
                "lastPhase": "Cloned",
                "cloneOperationName": None, # Remove clone name on success
            }
            # Clean up potential old status fields
            if "virtualMachineRef" in patch.status: del patch.status["virtualMachineRef"]
            if "startTime" in patch.status: del patch.status["startTime"]
            if "expiryTime" in patch.status: del patch.status["expiryTime"]
            return # Clone successful

        elif clone_phase == "Failed":
            logger.error(f"Clone '{clone_op_name}' failed. Check clone object status for details.")
            # Update status to reflect failure
            patch.status["cyberdesk_create"] = {
                 **current_status, # Keep existing fields if any
                 "lastPhase": "CloneFailed",
                 "cloneOperationName": None, # Remove clone name on failure
                 "virtualMachineRef": None, # Ensure no vmRef
            }
            raise kopf.PermanentError(f"Clone {clone_op_name} failed.")

        elif clone_phase == "Unknown":
                logger.warning(f"Clone '{clone_op_name}' phase is Unknown. Retrying status check...")
                raise kopf.TemporaryError(f"Clone {clone_op_name} phase Unknown.", delay=clone_wait_delay)
        else: # InProgress phases (SnapshotInProgress, CreatingTargetVM, RestoreInProgress, None)
            logger.info(f"Clone '{clone_op_name}' in progress ({clone_phase}). Waiting...")
            # Check for timeout only if clone is still in progress
            if retry >= max_clone_wait_retries: # Use >= for safety
                logger.error(f"Clone '{clone_op_name}' did not succeed within {max_clone_wait_retries} attempts.")
                # Attempt to delete the stuck clone object
                try:
                    CUSTOM_OBJECTS_API.delete_namespaced_custom_object(CLONE_GROUP, CLONE_VERSION, namespace, CLONE_PLURAL, clone_op_name)
                    logger.info(f"Deleted timed-out clone object '{clone_op_name}'.")
                except ApiException as del_exc:
                    if del_exc.status != 404:
                        logger.warning(f"Failed to delete timed-out clone object '{clone_op_name}': {del_exc.reason}")
                # Update status and mark as permanent failure
                patch.status["cyberdesk_create"] = {
                     **current_status,
                     "lastPhase": "CloneTimeout",
                     "cloneOperationName": None,
                     "virtualMachineRef": None,
                }
                raise kopf.PermanentError(f"Clone {clone_op_name} timed out.")
            else:
                # Still in progress and within retry limit, raise TemporaryError to retry
                raise kopf.TemporaryError(f"Clone {clone_op_name} in progress ({clone_phase}). Waiting...", delay=clone_wait_delay)

    except kopf.TemporaryError:
        raise # Propagate temporary errors for retry
    except kopf.PermanentError:
        raise # Propagate permanent errors
    except ApiException as e:
        # Catch API errors during clone check/creation
        logger.error(f"API error during clone processing for '{clone_op_name}': {e.reason}")
        raise kopf.TemporaryError(f"API error processing clone {clone_op_name}", delay=clone_wait_delay) from e
    except Exception as e:
        # Catch unexpected errors
        logger.exception(f"Unexpected error during reconciliation of '{instance_id}' at clone check state.") # Use logger.exception to include traceback
        raise kopf.TemporaryError(f"Unexpected error for {instance_id}: {e}", delay=30)


@kopf.on.field(KUBEVIRT_GROUP, KUBEVIRT_VERSION, KUBEVIRT_VMI_PLURAL, field="status.phase")
def vmi_phase_change(old: str | None, new: str | None, meta: dict, status: dict, logger: kopf.Logger, **_: Dict[str, object]):
    """Sync Supabase when a VMI phase flips, ignoring expected warm pool VMs."""
    if new is None:
        return  # nothing to do

    labels = meta.get("labels", {})
    vm_name = meta.get("name", "unknown-vmi")

    # Only process VMIs managed by or intended for this operator
    if labels.get("app") != "cyberdesk":
        logger.debug(f"Ignoring phase change for VMI {vm_name} (missing 'app: cyberdesk' label)")
        return

    instance_id = labels.get("cyberdesk-instance")

    if not instance_id:
        # If no instance ID, check if it's expected (part of warm pool)
        is_warm_pool_vm = (
            labels.get("pool.kubevirt.io/warm") == "ready" or
            labels.get("pool.kubevirt.io/in-use") == "true"
        )
        if is_warm_pool_vm:
            # Expected state for a VM in the pool or just assigned
            logger.debug(f"Ignoring phase change for warm pool VMI {vm_name} (no instance ID yet)")
        else:
            # Unexpected: Has 'app: cyberdesk' but no instance ID and no pool labels
            logger.warning(f"VMI {vm_name} has 'app: cyberdesk' but is missing 'cyberdesk-instance' label and doesn't appear to be a warm pool VM.")
        return # Don't proceed to Supabase update

    # --- Proceed with Supabase update only if we have an instance_id ---
    logger.info(f"Processing phase change ('{old}' -> '{new}') for VMI {vm_name} linked to instance {instance_id}")
    try:
        current_db = get_instance_status(instance_id)
        # Added check to prevent infinite loops if status already matches
        # This check requires get_instance_status to be relatively quick
        try:
             phase_enum = KubeVirtVMIPhase(new)
             desired = VMI_PHASE_TO_SUPABASE_STATUS.get(phase_enum, SupabaseInstanceStatus.ERROR).value
        except ValueError:
             logger.error(f"Unknown VMI phase '{new}' for {vm_name} -> marking ERROR in Supabase")
             desired = SupabaseInstanceStatus.ERROR.value

        if current_db != desired:
            logger.info(f"Supabase status mismatch for {instance_id} (DB: {current_db}, VMI wants: {desired}). Updating.")
            update_instance_status(instance_id, new)
        else:
             logger.debug(f"Supabase status for {instance_id} already matches desired state ({desired}). No update needed.")

    except Exception as e:
         # Catch potential errors during DB check/update
         logger.exception(f"Error processing VMI phase change for {instance_id} in Supabase: {e}")


@kopf.on.delete(CYBERDESK_GROUP, CYBERDESK_VERSION, CYBERDESK_PLURAL)
def cyberdesk_delete(meta: dict, body: dict, logger: kopf.Logger, **_: Dict[str, object]):
    """Tear down the associated VM when *Cyberdesk* is deleted, if provisioned."""
    instance_id = meta["name"]
    namespace = KUBEVIRT_NAMESPACE
    logger.info(f"Handling deletion for Cyberdesk CR '{instance_id}'.")

    vm_name = body.get("status", {}).get("cyberdesk_create", {}).get("virtualMachineRef")

    if vm_name:
        logger.info(f"Found virtualMachineRef '{vm_name}' in status. Attempting VM deletion.")
        try:
            CUSTOM_OBJECTS_API.delete_namespaced_custom_object(
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=namespace,
                plural=KUBEVIRT_VM_PLURAL,
                name=vm_name,
                # Optional: Add grace period if needed
                # body=kubernetes.client.V1DeleteOptions(grace_period_seconds=0) # Example: Force immediate deletion
            )
            logger.info(f"Successfully initiated deletion for VM '{vm_name}'.")
            # Removed cloud-init secret deletion attempt
        except ApiException as exc:
            if exc.status not in (404, 410): # Ignore if already deleted
                logger.error(f"Failed to delete VM '{vm_name}' during cleanup: {exc.status} {exc.reason}")
                raise kopf.TemporaryError(f"VM cleanup failed for {vm_name}, will retry", delay=15) from exc
            else:
                logger.info(f"VM '{vm_name}' already deleted or not found.")
    else:
        # --- Handle case where deletion happens before provisioning completes ---
        logger.warning(f"No virtualMachineRef found in status for deleted Cyberdesk '{instance_id}'. Provisioning may not have completed.")
        clone_op_name = body.get("status", {}).get("cyberdesk_create", {}).get("cloneOperationName")
        if clone_op_name:
            logger.info(f"Found cloneOperationName '{clone_op_name}'. Attempting to delete potentially lingering clone operation.")
            try:
                CUSTOM_OBJECTS_API.delete_namespaced_custom_object(
                    group=CLONE_GROUP,
                    version=CLONE_VERSION,
                    namespace=namespace,
                    plural=CLONE_PLURAL,
                    name=clone_op_name,
                )
                logger.info(f"Successfully deleted potentially lingering clone operation '{clone_op_name}'.")
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Failed to delete clone operation '{clone_op_name}' during cleanup: {e.status} {e.reason}. Manual check might be needed.")
                else:
                    logger.debug(f"Lingering clone operation '{clone_op_name}' not found.")
        else:
            logger.info(f"No cloneOperationName found either for '{instance_id}'. No KubeVirt resources to clean up based on status.")


@kopf.on.timer(CYBERDESK_GROUP, CYBERDESK_VERSION, CYBERDESK_PLURAL, interval=60)
def cyberdesk_timeout_check(body: dict, logger: kopf.Logger, **_: Dict[str, object]): # Added logger
    """Per‑resource timer: shut down VM once *expiryTime* passes."""
    expiry_str = body.get("status", {}).get("cyberdesk_create", {}).get("expiryTime")
    instance_id = body["metadata"]["name"]
    namespace = body["metadata"]["namespace"]

    if not expiry_str:
        logger.debug(f"No expiryTime found in status for '{instance_id}', skipping timeout check.")
        return

    try:
        expiry_dt = datetime.fromisoformat(expiry_str)
        if datetime.now(UTC) >= expiry_dt:
            logger.info(f"Cyberdesk '{instance_id}' expired at {expiry_str} — deleting CR.")
            # Deleting the CR will trigger the cyberdesk_delete handler for actual VM cleanup
            CUSTOM_OBJECTS_API.delete_namespaced_custom_object(
                group=CYBERDESK_GROUP,
                version=CYBERDESK_VERSION,
                namespace=namespace,
                plural=CYBERDESK_PLURAL,
                name=instance_id
            )
        # else: logger.debug(f"Cyberdesk '{instance_id}' not expired yet.")
    except ValueError:
         logger.error(f"Could not parse expiryTime '{expiry_str}' for '{instance_id}'.")
    except ApiException as e:
         logger.error(f"API error deleting expired Cyberdesk CR '{instance_id}': {e.reason}")
         # Raise temporary error to retry deletion
         raise kopf.TemporaryError(f"Failed to delete expired CR {instance_id}", delay=30) from e


# NEW Field Watcher for VMI Readiness (Post Cloud-Init)
@kopf.on.field(KUBEVIRT_GROUP, KUBEVIRT_VERSION, KUBEVIRT_VMI_PLURAL, field='status.conditions')
def vmi_ready_watcher(old, new, status, meta, logger: kopf.Logger, **kwargs):
    """Notify gateway when a VMI's Ready condition becomes True after cloud-init."""
    if not new: # Field might be cleared on deletion
        return

    labels = meta.get("labels", {})
    vm_name = meta.get("name", "unknown-vmi")

    # Filter for VMIs managed by us AND that have an instance ID
    if labels.get("app") != "cyberdesk" or not labels.get("cyberdesk-instance"):
        # logger.debug(f"Ignoring condition change for VMI {vm_name} (not a managed cyberdesk instance).")
        return

    instance_id = labels["cyberdesk-instance"]

    # Find the 'Ready' condition in the new status
    ready_condition = None
    for condition in new:
        if condition.get("type") == "Ready":
            ready_condition = condition
            break

    if not ready_condition:
        # logger.debug(f"No 'Ready' condition found in status update for VMI {vm_name}.")
        return

    is_ready = ready_condition.get("status") == "True"
    # logger.debug(f"VMI {vm_name} Ready condition status: {ready_condition.get('status')}")

    # Check if the old status also had Ready=True to avoid re-notifying
    was_ready = False
    if old:
        for condition in old:
             if condition.get("type") == "Ready" and condition.get("status") == "True":
                  was_ready = True
                  break

    if is_ready and not was_ready:
        logger.info(f"VMI '{vm_name}' ({instance_id}) condition changed to Ready=True. Notifying gateway.")

        # --- Notify Gateway --- #
        if not GATEWAY_BASE_URL:
            logger.warning(f"Gateway base URL not configured (In cluster? {IS_IN_CLUSTER}). Skipping notification for ready instance {instance_id}.")
        else:
            gateway_url = f"{GATEWAY_BASE_URL}/cyberdesk/{instance_id}/ready"
            logger.info(f"Notifying gateway for ready instance '{instance_id}' at {gateway_url}")
            try:
                # Using a simple synchronous request here for simplicity in handler
                # Consider making it async if gateway calls become slow/blocking
                req = urllib.request.Request(gateway_url, method="POST")
                # Add a timeout to prevent blocking indefinitely
                with urllib.request.urlopen(req, timeout=10) as response:
                    logger.info(f"Gateway notified successfully for ready '{instance_id}', status: {response.status}")
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError) as e:
                # Log error, but don't fail the handler - the VMI *is* ready
                logger.error(f"Failed to notify gateway for ready '{instance_id}': {e}")
            except Exception as e:
                 # Catch unexpected errors during notification
                 logger.exception(f"Unexpected error notifying gateway for ready '{instance_id}': {e}")
    # else:
         # logger.debug(f"VMI {vm_name} Ready condition did not change to True, or was already True. No action.")