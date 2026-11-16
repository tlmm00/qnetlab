#!/usr/bin/env python3
"""
ETSI GS QKD 014 V1.1.1 — Flask KME Server
HTTPS with mutual TLS (mTLS) — no passwords, no pre-shared secrets.
Keys are generated and synchronized with peer KMEs using shared seeds (KMA layer).
KSA layer handles client SAE requests.

Configuration is loaded from a YAML file (default: ./kme_config.yaml).

Endpoints:
  GET  /api/v1/keys/<slave_SAE_ID>/status
  POST /api/v1/keys/<slave_SAE_ID>/enc_keys   (master SAE — get new keys)
  GET  /api/v1/keys/<slave_SAE_ID>/enc_keys   (master SAE — simple GET form)
  POST /api/v1/keys/<master_SAE_ID>/dec_keys  (slave SAE — retrieve by key ID)
  GET  /api/v1/keys/<master_SAE_ID>/dec_keys  (slave SAE — single key_ID in URL)

KMA Internal Endpoint:
  POST /api/v1/kma/sync        - Internal endpoint for KMA key synchronization
"""

import argparse
import base64
import uuid
import sqlite3
import ssl
import logging
import os
import threading
import time
import hashlib
import json
import urllib.request
import urllib.error
from typing import Dict, Optional

import yaml
import werkzeug.serving
from flask import Flask, request, jsonify


# ── Configuration loader ─────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    """Read and validate the YAML configuration file."""
    with open(path, "r") as fh:
        cfg = yaml.safe_load(fh)

    # ── required top-level keys ──
    for key in ("listen", "local_kme_id", "tls", "storage", "peer_sync", "kme_topology"):
        if key not in cfg:
            raise SystemExit(f"Config error: missing required key '{key}' in {path}")

    # ── tls section ──
    tls = cfg["tls"]
    for key in ("server_cert_path", "server_key_path", "client_ca_cert_path"):
        if key not in tls:
            raise SystemExit(f"Config error: missing tls.{key}")

    # ── storage section ──
    storage = cfg["storage"]
    if storage.get("type") != "sqlite":
        raise SystemExit(f"Config error: unsupported storage type '{storage.get('type')}' (only 'sqlite' is supported)")
    if "path" not in storage.get("params", {}):
        raise SystemExit("Config error: missing storage.params.path")

    # ── peer_sync section ──
    ps = cfg["peer_sync"]
    if ps.get("type") != "http_mtls":
        raise SystemExit(f"Config error: unsupported peer_sync type '{ps.get('type')}'")
    for peer_id, peer_cfg in ps.get("peers", {}).items():
        for key in ("base_url", "seed", "client_cert_path", "client_key_path", "server_ca_cert_path"):
            if key not in peer_cfg:
                raise SystemExit(f"Config error: missing peer_sync.peers.{peer_id}.{key}")

    # ── kme_topology section ──
    if cfg["local_kme_id"] not in cfg["kme_topology"]:
        raise SystemExit(
            f"Config error: local_kme_id '{cfg['local_kme_id']}' not found in kme_topology"
        )

    return cfg


# ── Globals populated after config load ───────────────────────────────────────

CFG: dict = {}
KME_ID: str = ""
DEFAULT_KEY_SIZE_BITS  = 256
MAX_KEY_SIZE_BITS      = 1024
MIN_KEY_SIZE_BITS      = 64
MAX_KEY_COUNT          = 100_000
MAX_KEYS_PER_REQUEST   = 128

# Synchronization lock to prevent multiple sync threads running simultaneously
SYNC_LOCK = threading.Lock()

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)


# ── mTLS: custom Werkzeug request handler ─────────────────────────────────────
class MtlsRequestHandler(werkzeug.serving.WSGIRequestHandler):
    def make_environ(self):
        environ = super().make_environ()
        try:
            peer_cert = self.connection.getpeercert()
            for rdn in peer_cert.get("subject", []):
                for attr, val in rdn:
                    if attr == "commonName":
                        environ["SSL_CLIENT_CN"] = val
                        break
        except Exception:
            environ["SSL_CLIENT_CN"] = "unknown"
        return environ


# ── Database helpers ──────────────────────────────────────────────────────────

def _db_path() -> str:
    return CFG["storage"]["params"]["path"]


def get_db():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db_dir = os.path.dirname(_db_path())
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with get_db() as conn:
        # Legacy table kept for backward compatibility if needed, but not actively used for new sync logic
        conn.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                key_id           TEXT PRIMARY KEY,
                master_sae_id    TEXT NOT NULL,
                slave_sae_id     TEXT NOT NULL,
                key_data         TEXT NOT NULL,
                key_size_bits    INTEGER NOT NULL,
                delivered_master INTEGER DEFAULT 0,
                delivered_slave  INTEGER DEFAULT 0
            )
        """)

        # New Tables for KMA/KSA Sublayer Architecture
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kma_links (
                peer_kme_id TEXT PRIMARY KEY,
                seed TEXT NOT NULL,
                peer_url TEXT NOT NULL,
                counter INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sae_routes (
                sae_id TEXT PRIMARY KEY,
                peer_kme_id TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kma_keys (
                key_id TEXT PRIMARY KEY,
                peer_kme_id TEXT NOT NULL,
                key_data TEXT NOT NULL,
                key_size_bits INTEGER NOT NULL,
                status TEXT NOT NULL
            )
        """)
        conn.commit()

    log.info("Database initialised at %s", _db_path())


def seed_config_into_db():
    """Populate kma_links and sae_routes from the YAML configuration."""
    peers = CFG["peer_sync"].get("peers", {})
    topology = CFG["kme_topology"]
    local_id = CFG["local_kme_id"]

    with get_db() as conn:
        # ── Seed KMA peer links ──
        for peer_id, peer_cfg in peers.items():
            conn.execute(
                """INSERT OR REPLACE INTO kma_links
                   (peer_kme_id, seed, peer_url, counter)
                   VALUES (?, ?, ?, COALESCE(
                       (SELECT counter FROM kma_links WHERE peer_kme_id = ?), 0
                   ))""",
                (peer_id, peer_cfg["seed"], peer_cfg["base_url"], peer_id),
            )
            log.info("KMA link seeded for peer %s at %s", peer_id, peer_cfg["base_url"])

        # ── Seed SAE routes from topology ──
        # SAEs listed under a *remote* KME become routing targets pointing to
        # that KME.  SAEs listed under the *local* KME are local and don't need
        # a route entry (they connect directly to this server).
        for kme_id, kme_entry in topology.items():
            if kme_id == local_id:
                continue  # skip local — these SAEs are directly connected
            for sae_id in kme_entry.get("connected_saes", []):
                conn.execute(
                    "INSERT OR REPLACE INTO sae_routes (sae_id, peer_kme_id) VALUES (?, ?)",
                    (sae_id, kme_id),
                )
                log.info("SAE route seeded: %s -> %s", sae_id, kme_id)

        conn.commit()


def count_stored_keys(master_sae_id: str, slave_sae_id: str) -> int:
    with get_db() as conn:
        route = conn.execute("SELECT peer_kme_id FROM sae_routes WHERE sae_id=?", (slave_sae_id,)).fetchone()
        if not route:
            return 0
        peer_kme_id = route["peer_kme_id"]
        row = conn.execute(
            "SELECT COUNT(*) FROM kma_keys WHERE peer_kme_id=? AND status='synchronized'",
            (peer_kme_id,),
        ).fetchone()
    return row[0]

def get_key_count_for_peer(peer_id: str) -> int:
    """Get the count of synchronized keys for a specific peer."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM kma_keys WHERE peer_kme_id=? AND status='synchronized'",
            (peer_id,)
        ).fetchone()
        return row[0] if row else 0

def get_peer_counter(peer_id: str) -> int:
    """Get the current counter value for a peer."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT counter FROM kma_links WHERE peer_kme_id=?",
            (peer_id,)
        ).fetchone()
        return row["counter"] if row else 0

def increment_peer_counter(peer_id: str) -> int:
    """Increment the counter for a peer and return the new value."""
    with get_db() as conn:
        # Get current counter
        row = conn.execute(
            "SELECT counter FROM kma_links WHERE peer_kme_id=?",
            (peer_id,)
        ).fetchone()
        if not row:
            return 0
        new_counter = row["counter"] + 1
        conn.execute(
            "UPDATE kma_links SET counter = ? WHERE peer_kme_id=?",
            (new_counter, peer_id)
        )
        conn.commit()
        return new_counter

# ── KMA Sublayer Tasks ────────────────────────────────────────────────────────

def _build_peer_ssl_context(peer_cfg: dict) -> ssl.SSLContext:
    """Build a TLS context for outgoing connections to a specific peer."""
    ctx = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=peer_cfg["server_ca_cert_path"],
    )
    ctx.load_cert_chain(
        certfile=peer_cfg["client_cert_path"],
        keyfile=peer_cfg["client_key_path"],
    )
    # Disable hostname verification for IP-based connections
    ctx.check_hostname = False
    return ctx

def _build_fresh_ssl_context(peer_cfg: dict) -> ssl.SSLContext:
    """Build a FRESH TLS context for each connection to avoid stale sessions."""
    ctx = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH,
        cafile=peer_cfg["server_ca_cert_path"],
    )
    ctx.load_cert_chain(
        certfile=peer_cfg["client_cert_path"],
        keyfile=peer_cfg["client_key_path"],
    )
    ctx.check_hostname = False
    # Use a compatible cipher list
    ctx.set_ciphers('DEFAULT@SECLEVEL=1')
    return ctx

def sync_key_with_peer(peer_id: str, peer_cfg: dict, timeout_s: float) -> bool:
    """
    Synchronize a single key with a peer.
    Returns True if successful, False otherwise.
    """
    try:
        # Get current counter value and increment it
        counter = increment_peer_counter(peer_id)
        if counter == 0:
            log.warning("Could not increment counter for peer %s", peer_id)
            return False

        seed = peer_cfg["seed"]
        peer_url = peer_cfg["base_url"]
        full_url = f"{peer_url}/api/v1/kma/sync"

        log.debug("Syncing key with %s (counter: %d) to %s", peer_id, counter, full_url)

        # Generate deterministic key material
        raw_data = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        kdat = base64.b64encode(raw_data).decode()
        hash_val = hashlib.sha256(kdat.encode()).hexdigest()
        kid = str(uuid.uuid4())

        payload = {
            "qkd_key_id": kid,
            "peer_kme_id": KME_ID,
            "counter": counter,
            "hash_value": hash_val,
            "key_size_bits": DEFAULT_KEY_SIZE_BITS
        }

        # Build fresh TLS context for each request
        ctx = _build_fresh_ssl_context(peer_cfg)

        # Create request
        req = urllib.request.Request(
            full_url,
            data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )

        # Execute request with timeout
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_s) as resp:
            response_body = resp.read().decode()
            log.debug("Sync with %s returned status %d, body: %s",
                     peer_id, resp.status, response_body[:100] if response_body else "empty")

            if resp.status == 200:
                # Store the key locally
                with get_db() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO kma_keys
                           (key_id, peer_kme_id, key_data, key_size_bits, status)
                           VALUES (?, ?, ?, ?, 'synchronized')""",
                        (kid, peer_id, kdat, DEFAULT_KEY_SIZE_BITS)
                    )
                    conn.commit()
                log.debug("KMA synced key %s with %s (counter: %d)", kid, peer_id, counter)
                return True
            else:
                log.warning("Sync with %s returned non-200 status: %d", peer_id, resp.status)
                return False

    except urllib.error.URLError as e:
        log.exception("URLError during sync with %s (counter: %d)", peer_id, counter)
        return False
    except Exception as e:
        log.exception("Unexpected error during sync with %s", peer_id)
        return False

def kma_sync_task():
    """Background task to proactively synchronize keys with peer KMEs."""
    log.info("KMA Sync background task started. Thread ID: %s", threading.current_thread().ident)

    sync_interval = CFG["peer_sync"].get("sync_interval_s", 5)
    sync_threshold = CFG["peer_sync"].get("sync_threshold", 100)
    timeout_ms = CFG["peer_sync"].get("request_timeout_ms", 5000)
    timeout_s = timeout_ms / 1000.0
    max_retries = CFG["peer_sync"].get("max_retries", 3)
    peers_cfg = CFG["peer_sync"].get("peers", {})

    # Build TLS contexts per peer
    peer_contexts: Dict[str, ssl.SSLContext] = {}
    for peer_id, peer_cfg in peers_cfg.items():
        peer_contexts[peer_id] = _build_peer_ssl_context(peer_cfg)

    while True:
        try:
            # Use a lock to prevent multiple sync threads
            if not SYNC_LOCK.acquire(blocking=False):
                log.debug("Another sync task is already running, skipping this iteration")
                time.sleep(sync_interval)
                continue

            try:
                time.sleep(sync_interval)

                # Get list of peers to sync with
                with get_db() as conn:
                    links = conn.execute("SELECT * FROM kma_links").fetchall()

                if not links:
                    log.debug("No KMA links found in database")
                    continue

                log.debug("KMA sync iteration starting, found %d links", len(links))

                for link in links:
                    peer_id = link["peer_kme_id"]

                    # Only initiate sync if this KME has the smaller ID
                    if KME_ID >= peer_id:
                        log.debug("Skipping sync with %s (KME_ID %s >= %s)", peer_id, KME_ID, peer_id)
                        continue

                    # Get peer configuration
                    peer_cfg = peers_cfg.get(peer_id)
                    if not peer_cfg:
                        log.warning("No configuration found for peer %s", peer_id)
                        continue

                    # Check current key count
                    current_count = get_key_count_for_peer(peer_id)
                    log.debug("Current synchronized key count for %s: %d (threshold: %d)",
                             peer_id, current_count, sync_threshold)

                    if current_count >= sync_threshold:
                        log.debug("Key count for %s is at threshold, skipping sync", peer_id)
                        continue

                    # Calculate how many keys to generate
                    keys_to_generate = min(10, sync_threshold - current_count)
                    log.info("Generating %d keys for peer %s (current: %d, threshold: %d)",
                            keys_to_generate, peer_id, current_count, sync_threshold)

                    success_count = 0
                    for attempt in range(keys_to_generate):
                        # Retry logic with exponential backoff
                        for retry in range(max_retries):
                            if sync_key_with_peer(peer_id, peer_cfg, timeout_s):
                                success_count += 1
                                break
                            else:
                                if retry < max_retries - 1:
                                    wait_time = 2 ** retry
                                    log.warning("Sync attempt %d for %s failed, retrying in %ds...",
                                               retry + 1, peer_id, wait_time)
                                    time.sleep(wait_time)
                                else:
                                    log.error("All sync attempts for %s (attempt %d) failed",
                                             peer_id, attempt + 1)

                        # Small delay between key generations
                        generation_delay = CFG["peer_sync"].get("generation_delay_s", 0.5)
                        if attempt < keys_to_generate - 1 and generation_delay > 0:
                            time.sleep(generation_delay)

                    log.info("Successfully synced %d/%d keys with %s",
                            success_count, keys_to_generate, peer_id)

            finally:
                SYNC_LOCK.release()

        except Exception as e:
            log.exception("Exception in KMA sync task main loop: %s", e)
            # Release lock if held
            if SYNC_LOCK.locked():
                try:
                    SYNC_LOCK.release()
                except RuntimeError:
                    pass
            time.sleep(sync_interval)

# ── Route: KMA sync receiver ─────────────────────────────────────────────────

@app.route("/api/v1/kma/sync", methods=["POST"])
def kma_sync():
    """Receiver endpoint for KMA key synchronization."""
    try:
        body = request.get_json()
        if not body:
            return jsonify({"message": "Invalid JSON body"}), 400

        qkd_key_id = body.get("qkd_key_id")
        peer_kme_id = body.get("peer_kme_id")
        counter = body.get("counter")
        hash_value = body.get("hash_value")
        key_size_bits = body.get("key_size_bits")

        # Validate required fields
        if not all([qkd_key_id, peer_kme_id, counter is not None, hash_value]):
            return jsonify({"message": "Missing required fields"}), 400

        log.info("Received sync request from %s (counter: %d, key_id: %s)",
                peer_kme_id, counter, qkd_key_id[:8])

        with get_db() as conn:
            # Find the peer link
            link = conn.execute("SELECT * FROM kma_links WHERE peer_kme_id=?", (peer_kme_id,)).fetchone()
            if not link:
                log.warning("Unknown peer KME ID in sync: %s", peer_kme_id)
                return jsonify({"message": "Unknown KMA link"}), 404

            seed = link["seed"]

            # Generate identical key based on shared seed and counter
            raw_data = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
            kdat = base64.b64encode(raw_data).decode()
            my_hash = hashlib.sha256(kdat.encode()).hexdigest()

            if my_hash != hash_value:
                log.warning("KMA sync hash mismatch with %s (counter: %d)", peer_kme_id, counter)
                return jsonify({"message": "Hash mismatch"}), 400

            # Hash matches, store the key
            conn.execute(
                """INSERT OR IGNORE INTO kma_keys
                   (key_id, peer_kme_id, key_data, key_size_bits, status)
                   VALUES (?, ?, ?, ?, 'synchronized')""",
                (qkd_key_id, peer_kme_id, kdat, key_size_bits or DEFAULT_KEY_SIZE_BITS)
            )

            # Update local counter if the incoming counter is larger
            conn.execute(
                "UPDATE kma_links SET counter = ? WHERE peer_kme_id=? AND counter < ?",
                (counter, peer_kme_id, counter)
            )
            conn.commit()

            log.info("Successfully synchronized key %s with %s (counter: %d)",
                    qkd_key_id[:8], peer_kme_id, counter)
            return jsonify({"message": "Synchronized"}), 200

    except Exception as e:
        log.exception("Error in kma_sync endpoint: %s", e)
        return jsonify({"message": "Internal server error"}), 500

# ── Route: GET status (KSA Layer) ───────────────────────────────────────────────
@app.route("/api/v1/keys/<slave_sae_id>/status", methods=["GET"])
def get_status(slave_sae_id):
    master_sae_id = request.environ.get("SSL_CLIENT_CN", "unknown")
    stored = count_stored_keys(master_sae_id, slave_sae_id)

    # ETSI 014: target_KME_ID represents the target KME for the specified slave SAE
    target_kme_id = KME_ID
    with get_db() as conn:
        route = conn.execute("SELECT peer_kme_id FROM sae_routes WHERE sae_id=?", (slave_sae_id,)).fetchone()
        if route:
            target_kme_id = route["peer_kme_id"]

    log.info("GET status: master=%s slave=%s stored_keys=%d target_kme=%s",
             master_sae_id, slave_sae_id, stored, target_kme_id)

    return jsonify({
        "source_KME_ID":      KME_ID,
        "target_KME_ID":      target_kme_id,
        "master_SAE_ID":      master_sae_id,
        "slave_SAE_ID":       slave_sae_id,
        "key_size":           DEFAULT_KEY_SIZE_BITS,
        "stored_key_count":   stored,
        "max_key_count":      MAX_KEY_COUNT,
        "max_key_per_request": MAX_KEYS_PER_REQUEST,
        "max_key_size":       MAX_KEY_SIZE_BITS,
        "min_key_size":       MIN_KEY_SIZE_BITS,
        "max_SAE_ID_count":   0,  # Multicast not supported by this KME implementation
    }), 200


# ── Route: enc_keys (KSA Layer - master SAE requests new keys) ────────────────
@app.route("/api/v1/keys/<slave_sae_id>/enc_keys", methods=["GET", "POST"])
def enc_keys(slave_sae_id):
    master_sae_id = request.environ.get("SSL_CLIENT_CN", "unknown")

    body = {}
    if request.method == "POST" and request.is_json:
        body = request.get_json(silent=True) or {}

        # ETSI 014: Reject unsupported mandatory extensions with specific error message
        if "extension_mandatory" in body and isinstance(body["extension_mandatory"], list) and len(body["extension_mandatory"]) > 0:
            log.warning("enc_keys: Unhandled extension_mandatory received")
            return jsonify({
                "message": "not all extension_mandatory parameters are supported",
                "details": [{"extension_mandatory_unsupported": "No extensions are supported by this KME"}]
            }), 400

    number   = int(request.args.get("number",  body.get("number",  1)))
    key_size = int(request.args.get("size",    body.get("size",    DEFAULT_KEY_SIZE_BITS)))

    # ETSI 014: Size shall be a multiple of 8 check
    if key_size % 8 != 0:
        return jsonify({"message": "size shall be a multiple of 8"}), 400

    # Auto-convert if value is plausibly a byte count rather than bits
    if key_size <= MAX_KEY_SIZE_BITS // 8:
        log.warning(
            "enc_keys: 'size=%d' appears to be in bytes — converting to bits (%d). "
            "ETSI GS QKD 014 specifies key size in bits.",
            key_size, key_size * 8,
        )
        key_size = key_size * 8

    if number < 1 or number > MAX_KEYS_PER_REQUEST:
        return jsonify({"message": f"number must be 1–{MAX_KEYS_PER_REQUEST}"}), 400

    if key_size != DEFAULT_KEY_SIZE_BITS:
        return jsonify({"message": f"size must be {DEFAULT_KEY_SIZE_BITS} bits for KMA sync"}), 400

    keys_out = []
    with get_db() as conn:
        route = conn.execute("SELECT peer_kme_id FROM sae_routes WHERE sae_id=?", (slave_sae_id,)).fetchone()
        if not route:
            return jsonify({"message": "No KMA route found for slave SAE"}), 400

        peer_kme_id = route["peer_kme_id"]

        for _ in range(number):
            row = conn.execute(
                "SELECT key_id, key_data FROM kma_keys WHERE peer_kme_id=? AND status='synchronized' LIMIT 1",
                (peer_kme_id,)
            ).fetchone()

            if not row:
                conn.rollback()
                return jsonify({"message": "Not enough synchronized KMA-keys available"}), 503

            kid = row["key_id"]
            kdat = row["key_data"]

            # Mark the KMA-key as used (KSA layer taking ownership)
            conn.execute("UPDATE kma_keys SET status='used' WHERE key_id=?", (kid,))
            keys_out.append({"key_ID": kid, "key": kdat})

        conn.commit()

    log.info("enc_keys: master=%s slave=%s peer_kme=%s count=%d",
             master_sae_id, slave_sae_id, peer_kme_id, number)
    for k in keys_out:
        log.info("  key delivered to master: %s", k["key_ID"])

    return jsonify({"keys": keys_out}), 200


# ── Route: dec_keys (KSA Layer - slave SAE retrieves keys by ID) ──────────────
@app.route("/api/v1/keys/<master_sae_id>/dec_keys", methods=["GET", "POST"])
def dec_keys(master_sae_id):
    slave_sae_id = request.environ.get("SSL_CLIENT_CN", "unknown")

    if request.method == "POST" and request.is_json:
        body = request.get_json(silent=True) or {}
        # Parse arrays of objects correctly handling ETSI 014 Key IDs format
        requested_ids = [item["key_ID"] for item in body.get("key_IDs", []) if "key_ID" in item]
    else:
        # GET fallback handling single key_ID
        kid = request.args.get("key_ID")
        requested_ids = [kid] if kid else []

    if not requested_ids:
        return jsonify({"message": "no key_IDs supplied"}), 400

    keys_out = []
    missing  = []

    with get_db() as conn:
        for kid in requested_ids:
            row = conn.execute(
                "SELECT key_data, status FROM kma_keys WHERE key_id=?",
                (kid,)
            ).fetchone()

            # The key is valid if it exists and is either 'synchronized' or 'used' (if master got it first)
            if row is None or row["status"] not in ("synchronized", "used"):
                missing.append(kid)
            else:
                keys_out.append({"key_ID": kid, "key": row["key_data"]})
                conn.execute(
                    "UPDATE kma_keys SET status='used' WHERE key_id=?", (kid,)
                )

        if missing:
            conn.rollback()
            log.warning("dec_keys: key(s) not found: %s", missing)
            # ETSI 014 required error response for missing keys
            return jsonify({"message": "one or more keys specified are not found on KME"}), 400

        conn.commit()

    log.info("dec_keys: slave=%s master=%s count=%d",
             slave_sae_id, master_sae_id, len(keys_out))
    for k in keys_out:
        log.info("  key delivered to slave:  %s", k["key_ID"])

    return jsonify({"keys": keys_out}), 200


# ── TLS context ───────────────────────────────────────────────────────────────
def build_ssl_context() -> ssl.SSLContext:
    tls = CFG["tls"]
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=tls["server_cert_path"], keyfile=tls["server_key_path"])
    ctx.load_verify_locations(cafile=tls["client_ca_cert_path"])
    if CFG.get("use_mTLS", True):
        ctx.verify_mode = ssl.CERT_REQUIRED          # enforce mutual TLS
    else:
        ctx.verify_mode = ssl.CERT_OPTIONAL
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ETSI QKD 014 KME Server")
    parser.add_argument(
        "--config", "-c",
        default=os.environ.get("KME_CONFIG", "./kme_config.yaml"),
        help="Path to YAML configuration file (default: ./kme_config.yaml or $KME_CONFIG)",
    )
    args = parser.parse_args()

    # ── Load configuration ──
    CFG = load_config(args.config)
    KME_ID = CFG["local_kme_id"]

    # ── Configure logging ──
    log_cfg = CFG.get("logging", {})
    log_level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file_path", "/var/log/kme.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    log = logging.getLogger("kme")

    log.info("Loaded configuration from %s", args.config)

    # ── Initialise database and seed config ──
    init_db()
    seed_config_into_db()

    # ── Start KMA Sync background task ──
    sync_thread = threading.Thread(target=kma_sync_task, daemon=True, name="KMASync")
    sync_thread.start()
    log.info("KMA Sync thread started (ID: %s)", sync_thread.ident)

    # ── Parse listen address ──
    listen = CFG["listen"]
    if ":" in listen:
        host, port_str = listen.rsplit(":", 1)
        port = int(port_str)
    else:
        host, port = "0.0.0.0", int(listen)

    log.info("KME server starting — KME_ID=%s  DB=%s  %s:%d",
             KME_ID, _db_path(), host, port)
    ssl_ctx = build_ssl_context()
    werkzeug.serving.run_simple(
        host,
        port,
        app,
        ssl_context=ssl_ctx,
        request_handler=MtlsRequestHandler,
        use_reloader=False,
    )
else:
    # When imported (e.g. for testing), set up a minimal logger
    log = logging.getLogger("kme")
