import os
import yaml
from dotenv import load_dotenv
from modules.predict_client import PredictClient

with open('config/account_1.config.yaml') as f:
    config = yaml.safe_load(f)

load_dotenv('config/account_1.env')
client = PredictClient(
    private_key=os.getenv('PREDICT_PRIVATE_KEY'),
    api_key=os.getenv('PREDICT_API_KEY'),
    wallet_address=config.get('wallet_address', os.getenv('PREDICT_WALLET_ADDRESS')),
    predict_account=config.get('predict_account')
)
print("Requesting balances...")
print(client.get_balances())
