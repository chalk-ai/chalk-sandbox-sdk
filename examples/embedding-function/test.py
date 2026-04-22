import chalkcompute

orchestrate = chalkcompute.RemoteFunction.from_name(name="chalk-sandbox-test-orchestrate")
orchestrate.wait_ready()
embeddings = orchestrate(["Hello", "world!"])
print(embeddings)
