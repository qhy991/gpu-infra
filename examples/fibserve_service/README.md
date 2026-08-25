# Broker-held FIBServe template

This task is a checked contract template, not a self-starting GPU service. Start
FIBServe as one foreground `gpu-run --mode exclusive` job, then create
`deployment.json` with `kernelctl service-attest`. Replace every identity
placeholder with the frozen evaluator, dataset, launch-wrapper, compatibility,
and task facts used by that deployment.

Every submitted candidate must contain the source path named by
`--entry-point`; this template expects `kernel.cu`. A service stage has no
`resources` block because its HTTP request does not allocate another GPU. Many
runs may enter concurrently; FIBServe serializes or schedules them on the
already broker-held worker.
