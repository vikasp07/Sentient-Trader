# model/train.py
import mlflow
import mlflow.sklearn
import xgboost as xgb
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

# Load the dataset (example, replace with actual features)
data = pd.read_csv("data/market_data_with_sentiment.csv")

# Feature engineering (replace with actual feature extraction)
X = data.drop(columns=["target"])
y = data["target"]

# Train/test split
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)

# Start MLflow tracking
mlflow.start_run()

# Train the XGBoost model
model = xgb.XGBClassifier()
model.fit(X_train, y_train)

# Log the model with MLflow
mlflow.sklearn.log_model(model, "xgboost_model")

# Make predictions and log metrics
predictions = model.predict(X_test)
accuracy = accuracy_score(y_test, predictions)
mlflow.log_metric("accuracy", accuracy)

# End the MLflow run
mlflow.end_run()
