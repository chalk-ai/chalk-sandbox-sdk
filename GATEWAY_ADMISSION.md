# Optional shared ingress admission

```python
import chalkcompute as cc

@cc.function(gateway_admission=cc.GatewayAdmissionPolicy(32))
def score(value: float) -> float:
    return value * 2
```

The shared request cap equals the number of routable replicas times the allowance.
Redis coordinates the budget across gateway Envoys. It does not guarantee equal
per-replica distribution. Functions, classes, and generic ScalingGroup accept this
policy; generic groups require native gRPC serving for execution lifetime.

Execution lifetime (the default) retains accepted work after caller cancellation
and requires a deployment image with the native lease protocol. Network lifetime
(`lifetime="network"`) protects upstream exchanges only. Both require the optional
coordinator to be configured on the selected dataplane gateway. Omit the policy
to retain the existing routing behavior.

`max_scale_from_zero_requests` optionally bounds wakeup requests for zero-replica
HOLD groups (default one). Queue consumers, direct bypass, and retained logical
input bytes remain governed by the separate local ConcurrencyPolicy.
