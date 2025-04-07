import os
import datetime
import json
# --- Added session and request ---
from flask import Flask, render_template, redirect, url_for, flash, request, session
from dotenv import load_dotenv

# --- Plaid Client Libraries ---
from plaid.api import plaid_api
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.configuration import Configuration
from plaid.api_client import ApiClient
import plaid # Import the base library for plaid.ApiException

# --- Load Environment Variables ---
load_dotenv()

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    print("Warning: FLASK_SECRET_KEY not set in .env. Using default, insecure key.")
    app.secret_key = "default-dev-secret-key-change-me-in-production"


# --- Plaid Configuration ---
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")
PLAID_ACCESS_TOKEN = os.getenv("PLAID_ACCESS_TOKEN_PRIMARY")

# --- Plaid Client Setup ---
# (Keep initialize_plaid_client function exactly as it was)
def initialize_plaid_client():
    """Initializes and returns the Plaid API client using environment URL strings."""
    try:
        environment_urls = {
            'sandbox': 'https://sandbox.plaid.com',
            'development': 'https://development.plaid.com',
            'production': 'https://production.plaid.com',
        }
        plaid_env_key = PLAID_ENV.lower() if PLAID_ENV else 'sandbox'
        host_url = environment_urls.get(plaid_env_key)

        if host_url is None:
            valid_envs = ", ".join(environment_urls.keys())
            error_msg = f"Error: Invalid PLAID_ENV value '{PLAID_ENV}' in .env file. Use one of: {valid_envs}"
            print(error_msg)
            flash(f"Invalid Plaid environment configured: '{PLAID_ENV}'. Use one of: {valid_envs}", "danger")
            return None

        configuration = Configuration(
            host=host_url,
            api_key={
                'clientId': PLAID_CLIENT_ID,
                'secret': PLAID_SECRET,
            }
        )
        if not PLAID_CLIENT_ID or not PLAID_SECRET:
             print("Error: PLAID_CLIENT_ID or PLAID_SECRET not found in .env file.")
             flash("Plaid Client ID or Secret missing in configuration. Check .env file.", "danger")
             return None

        api_client = ApiClient(configuration)
        client = plaid_api.PlaidApi(api_client)
        print(f"Plaid client initialized successfully for {plaid_env_key} environment.")
        return client
    except Exception as e:
        error_msg = f"Error initializing Plaid client: {e}"
        print(error_msg)
        import traceback
        traceback.print_exc()
        flash(f"Error initializing Plaid client. Check terminal logs.", "danger")
        return None

# --- Plaid Data Fetching Function ---
# (Keep get_plaid_transactions function exactly as it was)
def get_plaid_transactions(client, access_token, start_date, end_date):
    """
    Fetches transactions from Plaid for a given access token and date range.
    Returns a list of formatted transactions or None on error.
    """
    if not client or not access_token:
        flash("Plaid client or access token is missing. Check .env configuration.", "danger")
        return None
    try:
        accounts_request = AccountsGetRequest(access_token=access_token)
        accounts_response = client.accounts_get(accounts_request)
        account_map = {acc.account_id: acc.name for acc in accounts_response['accounts']}
        print(f"Account map: {account_map}")

        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date.date(),
            end_date=end_date.date(),
            options=TransactionsGetRequestOptions(
                count=500,
                offset=0
            )
        )
        response = client.transactions_get(request)
        transactions_result = response['transactions']

        while len(transactions_result) < response['total_transactions']:
             request.options.offset = len(transactions_result)
             response = client.transactions_get(request)
             transactions_result.extend(response['transactions'])

        print(f"Fetched {len(transactions_result)} transactions from Plaid.")

        formatted_transactions = []
        for t in transactions_result:
            if t['pending']:
                continue
            category = ' > '.join(t['category']) if t['category'] else 'Uncategorized'
            account_name = account_map.get(t['account_id'], 'Unknown Account')
            amount = t['amount'] * -1
            formatted_transactions.append({
                'id': t['transaction_id'],
                'date': t['date'],
                'account': account_name,
                'name': t['name'],
                'amount': amount,
                'category': category
            })
        formatted_transactions.sort(key=lambda x: x['date'], reverse=True)
        return formatted_transactions
    except plaid.ApiException as e:
        error_response = json.loads(e.body)
        error_message = error_response.get('error_message', 'Unknown Plaid API error')
        error_code = error_response.get('error_code', 'UNKNOWN')
        print(f"Plaid API Error: {error_code} - {error_message}")
        flash(f"Error fetching data from Plaid: {error_message} ({error_code})", "danger")
        if error_code == 'ITEM_LOGIN_REQUIRED':
             flash("Bank connection needs update. Re-link account required.", "warning")
        return None
    except Exception as e:
        print(f"An unexpected error occurred fetching Plaid data: {e}")
        import traceback
        traceback.print_exc()
        flash(f"An unexpected error occurred: {e}", "danger")
        return None

# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main page, fetching and displaying transactions."""
    print("Route requested: / (index)")

    # --- Initialize session for excluded IDs as a LIST ---
    if 'excluded_ids' not in session:
        session['excluded_ids'] = [] # Use a list
    excluded_ids = session['excluded_ids'] # It's now a list
    # ---

    end_date = datetime.datetime.now()
    start_date = end_date - datetime.timedelta(days=30)
    days_param = 30
    try:
        days_param_str = request.args.get('days')
        if days_param_str:
             days_param = int(days_param_str)
             if days_param > 0:
                  start_date = end_date - datetime.timedelta(days=days_param)
             else:
                  days_param = 30
                  flash("Using default 30 days.", "warning")
    except ValueError:
        flash("Invalid 'days' parameter. Using default 30 days.", "warning")
        days_param = 30

    plaid_client = initialize_plaid_client()
    transactions = []
    total_income = 0.0
    total_expenses = 0.0
    balance = 0.0

    if plaid_client and PLAID_ACCESS_TOKEN:
        print(f"Fetching Plaid transactions from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        transactions_data = get_plaid_transactions(
            plaid_client,
            PLAID_ACCESS_TOKEN,
            start_date,
            end_date
        )
        if transactions_data is not None:
            transactions = transactions_data
            for t in transactions:
                # --- Check if transaction ID is excluded (membership check in list) ---
                if t['id'] in excluded_ids:
                    continue
                # ---

                if t['amount'] < 0:
                    total_income += abs(t['amount'])
                elif t['amount'] > 0:
                    total_expenses += t['amount']
            balance = total_income - total_expenses
    elif not PLAID_ACCESS_TOKEN:
         flash("PLAID_ACCESS_TOKEN_PRIMARY not found in .env file.", "danger")
    else:
        pass

    # Pass excluded_ids list to the template
    return render_template(
        'index.html',
        transactions=transactions,
        total_income=total_income,
        total_expenses=total_expenses,
        balance=balance,
        current_days=days_param,
        excluded_ids=excluded_ids # Pass the list of excluded IDs
        )

@app.route('/refresh', methods=['POST'])
def trigger_refresh():
    """Handles the request from the 'Refresh' button."""
    print("Route requested: /refresh (POST)")
    flash('Refreshing transaction data...', 'info')
    return redirect(url_for('index'))


@app.route('/exclude', methods=['POST'])
def exclude_transaction():
    """Adds a transaction ID to the excluded list in the session if not already present."""
    transaction_id = request.form.get('transaction_id')
    if transaction_id:
        # --- Work with the LIST ---
        if 'excluded_ids' not in session:
            session['excluded_ids'] = [] # Ensure it's a list

        current_excluded = session['excluded_ids'] # Get the current list

        # Only add if not already excluded
        if transaction_id not in current_excluded:
            current_excluded.append(transaction_id)
            session['excluded_ids'] = current_excluded # Reassign modified list to session
            print(f"Excluded transaction ID: {transaction_id}")
            flash(f'Transaction {transaction_id[:8]}... excluded from summary.', 'warning')
        else:
            print(f"Transaction ID {transaction_id} already excluded.")
            flash(f'Transaction {transaction_id[:8]}... was already excluded.', 'info')
        # --- End list modification ---
    else:
        flash('Could not exclude transaction: ID missing.', 'danger')

    days = request.args.get('days')
    if days:
         return redirect(url_for('index', days=days))
    else:
         return redirect(url_for('index'))


@app.route('/clear_exclusions', methods=['POST'])
def clear_exclusions():
    """Clears the list of excluded transaction IDs from the session."""
    if 'excluded_ids' in session and session['excluded_ids']: # Check if exists and is not empty
        session.pop('excluded_ids')
        print("Cleared all excluded transaction IDs.")
        flash('All transaction exclusions cleared.', 'info')
    else:
        flash('No exclusions to clear.', 'info')

    days = request.args.get('days')
    if days:
         return redirect(url_for('index', days=days))
    else:
         return redirect(url_for('index'))

# --- Run the App ---
if __name__ == '__main__':
    if not app.secret_key or app.secret_key == "default-dev-secret-key-change-me-in-production":
         print("\n*** WARNING: Flask Secret Key is not securely set! ***")
         print("*** Session data may not be secure. Set FLASK_SECRET_KEY in your .env file. ***\n")
    print("Starting Flask development server...")
    app.run(debug=True, port=5001)