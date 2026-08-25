# Broker-held FIBServe template

This task is a checked contract template, not a self-starting GPU service. Start
FIBServe as one foreground broker v0.6 `gpu-run --mode exclusive` job with
`--receipt-out`, then create `deployment.json` with `kernelctl service-attest
--broker-admission-receipt`. Replace every identity
placeholder with the frozen evaluator, dataset, launch-wrapper, compatibility,
and task facts used by that deployment. The judge identity also binds the
receipt's launch-spec and executable SHA-256 values.

Every submitted candidate must contain the source path named by
`--entry-point`; this template expects `kernel.cu`. A service stage has no
`resources` block because its HTTP request does not allocate another GPU. Many
runs may enter concurrently; FIBServe serializes or schedules them on the
already broker-held worker.
