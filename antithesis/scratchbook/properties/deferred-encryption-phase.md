---
sut_path: /home/colaya/src/customer/customer-percona/percona-xtradb-cluster
commit: f9ecb3ebe8ff4df5e9b931becea4f9bb640d79ae
updated: 2026-09-10
external_references:
  - path: https://docs.percona.com/percona-xtradb-cluster/8.4/
    why: Upstream product documentation (user-approved scope: repo + upstream docs)
  - path: https://galeracluster.com/library/documentation/
    why: Galera replication library documentation (user-approved scope: repo + upstream docs)
  - path: https://dev.mysql.com/doc/refman/8.4/en/
    why: MySQL 8.4 reference documentation (user-approved scope: repo + upstream docs)
---

# deferred-encryption-phase — Placeholder set for the encryption variant (NOT full properties)

**Status: deferred phase placeholder** (catalog "Phasing and variants", deferred phase
`encryption variant`; synthesis gap 6). These are three *named research targets*, not
worked properties — no invariant plans, no workload designs. They exist so that the v1
decision `pxc_encrypt_cluster_traffic=OFF` (deployment-topology.md — TLS sits below every
v1 target subsystem and obscures what network faults do to the wire protocol) is a
recorded *decision to defer*, not an oversight: the field default is **ON and
read-only**, so v1 has zero coverage of the default field configuration's
encryption-specific failure modes.

The phase needs its own image/config variant (certs + keyring component baked into the
image; two independently bootstrapped halves have mutually untrusted auto-generated CAs
and can never merge — `sql/ssl_init_callback.cc:506-548`, sut-analysis §8.2 — so cert
distribution is a harness design problem in itself).

## Placeholder items

1. **TLS rotation / `socket.ssl_reload`.** Galera-channel TLS is configured from the
   mysqld `ssl-*` variables exactly ONCE at startup (`sql/ssl_init_callback.cc:553-558`
   → `sql/wsrep_mysqld.cc:1219-1224`) and is NOT covered by `ALTER INSTANCE RELOAD TLS`;
   the provider has a separate knob, `socket.ssl_reload` (galerautils
   `gu_asio.cpp:570-600`). Rotating certs "the MySQL way" leaves the cluster channel on
   old certs → simultaneous expiry breaks ALL inter-node TLS at once. Also: no
   hostname/peer-identity verification on the Galera channel (`gu_asio.cpp:416-421`,
   verify_peer only). Target: rotation under load never partitions the cluster;
   `socket.ssl_reload` effect on established connections (sut-analysis open Q34).
2. **keyring × SST transition-key handling.** Donor checks joiner keyring compatibility
   via untimed SQL, joiner via filesystem JSON grep
   (`get_keyring_manifest_and_config`, `wsrep_sst_xtrabackup-v2.sh:1048-1105`);
   disagreement is detected only AFTER the transfer starts (`:2325-2350`). The SST
   transition key is generated in bash from `/dev/urandom|tr|fold|head` and written
   PLAINTEXT into sst_info (`:1968-1976`). Server side: keyring reload after SST at
   `sql/wsrep_mysqld.cc:1327-1330` (`wsrep_init_startup`). Target: keyring-mismatch SST
   fails cleanly (no half-transferred datadir, joiner retries or errors explicitly);
   transition-key lifecycle leaves no plaintext residue.
3. **gcache/disk-page encryption crash recovery.** gcache encryption is the ONLY
   combination axis in the entire MTR galera corpus (`galera_encryption` suite, 7 tests
   — sut-analysis §10.1) and is never crash-tested; the unencrypted gcache recovery
   path already has realized bugs (PXC-5209; catalog `gcache-crash-recovery-no-abort`,
   `gcache-recovered-ist-completeness`). Target: kill -9 with an encrypted
   galera.cache/page store recovers or resets cleanly (no abort loop), and a donor with
   crash-recovered encrypted gcache never serves broken IST — i.e., the existing gcache
   crash properties re-run on the encrypted variant.

## Why deferred (not in v1)

- v1 runs `pxc_encrypt_cluster_traffic=OFF` (topology decision): TLS adds handshake
  state and cert plumbing under every targeted subsystem without covering new target
  code, and the auto-generated-CA bootstrap makes multi-node harness setup materially
  harder. The §8.2 TLS findings are explicitly earmarked for this variant.
- Items 2 and 3 additionally depend on the kill channel (crash legs) and a
  keyring-component-configured image — both phase-2+ infrastructure.
- Nothing in v1's fault plan exercises encryption-specific code, so deferral costs no
  v1 coverage; the cost is zero coverage of the *field-default* config, which this file
  makes explicit.
