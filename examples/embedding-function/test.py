import chalkcompute

orchestrate = chalkcompute.RemoteFunction.from_name("orchestrate")
orchestrate.wait_ready()
embeddings = orchestrate(["Hello", "world!"])
print(embeddings)
