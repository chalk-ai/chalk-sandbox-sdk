import json

import pyarrow as pa
import pyarrow.compute as pc

from chalk.ml import model_handler


@model_handler
class AffineHandler:
    def load_model(self):
        with open(self.files["weights.json"]) as f:
            self.weights = json.load(f)

    def predict(self, df):
        x = df.to_arrow().column("x")
        y = pc.add(pc.multiply(x, self.weights["factor"]), self.weights["bias"])
        return pa.table({"y": y})
