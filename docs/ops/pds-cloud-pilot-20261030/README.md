# Cloud PDS pilot

Five conditional empty-field repairs completed on October 30, 2026, on a dedicated
Fly shared-cpu-1x/256 MiB machine in ewr. No HeavyPad work, corpus download, or
volume allocation. The pilot machine `185d5e7bd04568` was destroyed after receipt
collection; the repair app and its configured credentials remain for the next
bounded stage. Only a 2,359-byte compressed receipt archive was collected locally.

The dry-run passed. The first apply attempt used a read-only local credential and
received an explicit Turso BLOCKED rejection; readback remained unchanged. Its
ledger was retained. The canonical credential from the encrypted store was then
used with fresh observations for the successful run. No uncertain write was retried.

All five accounts passed source readback and public global/correct-PDS/wrong-PDS
searches. The run took 314.23 seconds, with individual write responses between
0.18 and 1.66 seconds and public visibility between 61.74 and 63.16 seconds.
The serving generation stayed ready; sampled sync lag was 21–46 seconds.
Five actors were changed; handles and other profile columns were not written.
The committed events contain the conditional write receipts and verification times.

Allocation: one shared CPU, 256 MiB, no persistent volume, approximately nine
minutes including setup/debugging. This is resource accounting, not an invoiced
cost figure. The sequential propagation waits are not representative bulk throughput.
The result permits a bounded 100-account trial, not a full-backlog capacity claim.
