# Broker-held FIBServe template

`service.json` is the daemon-managed launch contract. Replace its owner, paths,
port, source/evaluator/dataset/config identities, command, and environment, then
run `kernelctl service-start`. Once `service-wait` reports ready, the immutable
deployment directory owns `admission.json` and `deployment.json`.

`task.json` is the corresponding candidate-evaluation template. Materialize it
with `kernelctl service-bind-task --deployment ... --template task.json --out
bound-task.json`. The daemon live-verifies the deployment, replaces only the
two exact tokens, validates the result, and emits a sibling binding receipt.
The identity includes deployment id, deployment-receipt, launch-spec, and
executable SHA-256 values; the stage also records `service_deployment` for
consumer protection and optional idle-grace release.

For externally launched services, the lower-level alternative remains broker
v0.6 `gpu-run --receipt-out` followed by `kernelctl service-attest
--broker-admission-receipt`.

Every submitted candidate must contain the source path named by
`--entry-point`; this template expects `kernel.cu`. A service stage has no
`resources` block because its HTTP request does not allocate another GPU. Many
runs may enter concurrently; FIBServe serializes or schedules them on the
already broker-held worker.
