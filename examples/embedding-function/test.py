import chalkcompute

orchestrate = chalkcompute.RemoteFunction.from_name("ex-orchestrate")
orchestrate.wait_ready()
embeddings = orchestrate.remote(["Hello", "world!"])
print(embeddings)
