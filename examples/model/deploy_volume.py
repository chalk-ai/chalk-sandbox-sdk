"""Deploy a @model_handler that triples inputs.

uv run python examples/model/deploy_volume.py
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

from chalk.client import ChalkClient
from chalk.ml.model_handler import model_handler
from chalk.scalinggroup import AutoScalingSpec, ScalingGroupResourceRequest

c = ChalkClient(environment=os.environ.get("CHALK_ENVIRONMENT_ID"))

MODEL_NAME = "ex-volume-triple"
SG_NAME = "ex-volume-triple-sg"


@model_handler
class TriplingModel:
    def predict(self, df):
        return pd.DataFrame({"y": self.model.predict(df.to_pandas())})


X_train = np.array([[1.0], [2.0], [3.0], [4.0], [5.0]])
y_train = X_train.ravel() * 3.0
model = LinearRegression().fit(X_train, y_train)

v = c.register_model_version(
    name=MODEL_NAME,
    model=TriplingModel(model=model),
    input_schema={"x": float},
    output_schema={"y": float},
    dependencies=["scikit-learn", "pandas"],
)

c.deploy_model_version_to_scaling_group(
    name=SG_NAME,
    model_name=MODEL_NAME,
    model_version=v.model_version,
    scaling=AutoScalingSpec(
        min_replicas=1,
        max_replicas=1,
        target_cpu_utilization_percentage=70,
    ),
    resources=ScalingGroupResourceRequest(cpu="500m", memory="512Mi"),
)
