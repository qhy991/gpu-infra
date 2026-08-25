# Broker-held FIBServe template

`service.json` is the daemon-managed launch contract. Replace its owner, paths,
port, source/evaluator/dataset/config identities, command, and environment, then
run `kernelctl service-start`. Once `service-wait` reports ready, the immutable
deployment directory owns `admission.json` and `deployment.json`.

`task.json` is the corresponding candidate-evaluation template. Bind its judge
identity and deployment-receipt path to the ready deployment. The identity
includes the deployment's launch-spec and executable SHA-256 values.

For externally launched services, the lower-level alternative remains broker
v0.6 `gpu-run --receipt-out` followed by `kernelctl service-attest
--broker-admission-receipt`.

Every submitted candidate must contain the source path named by
`--entry-point`; this template expects `kernel.cu`. A service stage has no
`resources` block because its HTTP request does not allocate another GPU. Many
runs may enter concurrently; FIBServe serializes or schedules them on the
already broker-held worker.
