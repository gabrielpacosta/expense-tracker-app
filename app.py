import os
import datetime
import json
from flask import Flask, render_template, redirect, url_for, flash, request, session
from dotenv import load_dotenv

# --- Plaid Client Libraries ---
from plaid.api import plaid_api
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.configuration import Configuration
from plaid.api_client import ApiClient
import plaid

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
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox") # Default to sandbox if not set
PLAID_ACCESS_TOKEN = os.getenv("PLAID_ACCESS_TOKEN_PRIMARY")

# --- Helper Function to get dates ---
def get_date_range():
    """
    Determines the start and end dates.
    Default: Start is last Sunday, End is today.
    Overrides with query parameters 'start_date' and 'end_date' if valid.
    Returns (start_date_obj, end_date_obj) as datetime objects.
    """
    today = datetime.datetime.now()
    # Calculate default start date: last Sunday
    # Monday is 0, Sunday is 6. offset = days ago Sunday was.
    offset = (today.weekday() + 1) % 7
    default_start_date = today - datetime.timedelta(days=offset)
    default_end_date = today

    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    start_date_obj = default_start_date
    end_date_obj = default_end_date

    try:
        if start_date_str:
            start_date_obj = datetime.datetime.strptime(start_date_str, '%Y-%m-%d')
        if end_date_str:
            # Set end_date_obj to the very end of the selected day
            end_date_obj = datetime.datetime.strptime(end_date_str, '%Y-%m-%d') + datetime.timedelta(days=1, microseconds=-1)

        # Basic validation: end date should not be before start date
        if end_date_obj < start_date_obj:
             flash("End date cannot be before start date. Using default range.", "warning")
             start_date_obj = default_start_date
             end_date_obj = default_end_date

    except ValueError:
        flash("Invalid date format in URL. Please use YYYY-MM-DD. Using default range.", "warning")
        start_date_obj = default_start_date
        end_date_obj = default_end_date

    # Ensure start/end dates are time-zone naive or consistent if using timezones
    # For simplicity here, we assume naive datetimes from strptime

    # Return dates truncated to the beginning of the start day and end of the end day
    # Adjust start_date_obj to be the beginning of the day for clarity in display
    start_date_obj_display = start_date_obj.replace(hour=0, minute=0, second=0, microsecond=0)
    # Use the potentially adjusted end_date_obj for fetching, but display the selected date
    end_date_obj_display = datetime.datetime.strptime(end_date_str, '%Y-%m-%d') if end_date_str else default_end_date

    return start_date_obj, end_date_obj, start_date_obj_display, end_date_obj_display


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
    Accepts datetime objects, uses .date() for the API call.
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

        # Use .date() to pass only the date part to the API
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

    if 'excluded_ids' not in session:
        session['excluded_ids'] = []
    excluded_ids = session['excluded_ids']

    # --- Get Date Range (Default or from Query Params) ---
    start_date_obj, end_date_obj, start_date_display, end_date_display = get_date_range()
    # ---

    plaid_client = initialize_plaid_client()
    transactions = []
    total_income = 0.0
    total_expenses = 0.0
    balance = 0.0

    if plaid_client and PLAID_ACCESS_TOKEN:
        # Pass the potentially time-adjusted dates to the fetch function
        print(f"Fetching Plaid transactions from {start_date_obj.strftime('%Y-%m-%d')} to {end_date_obj.strftime('%Y-%m-%d')}")
        transactions_data = get_plaid_transactions(
            plaid_client,
            PLAID_ACCESS_TOKEN,
            start_date_obj,
            end_date_obj
        )
        if transactions_data is not None:
            transactions = transactions_data
            for t in transactions:
                if t['id'] in excluded_ids:
                    continue
                if t['amount'] < 0:
                    total_income += abs(t['amount'])
                elif t['amount'] > 0:
                    total_expenses += t['amount']
            balance = total_income - total_expenses
    elif not PLAID_ACCESS_TOKEN:
         flash("PLAID_ACCESS_TOKEN_PRIMARY not found in .env file.", "danger")
    else:
        pass

    # Pass display dates (YYYY-MM-DD) for input pre-filling
    return render_template(
        'index.html',
        transactions=transactions,
        total_income=total_income,
        total_expenses=total_expenses,
        balance=balance,
        # Pass formatted dates for the date inputs
        start_date_value=start_date_display.strftime('%Y-%m-%d'),
        end_date_value=end_date_display.strftime('%Y-%m-%d'),
        excluded_ids=excluded_ids
        )

@app.route('/refresh', methods=['POST'])
def trigger_refresh():
    """Handles the request from the 'Refresh' button. Redirects to index with current date range."""
    print("Route requested: /refresh (POST)")
    flash('Refreshing transaction data...', 'info')
    # Redirect back to index, preserving the current date range from request args
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('index', start_date=start_date, end_date=end_date))


@app.route('/exclude', methods=['POST'])
def exclude_transaction():
    """Adds a transaction ID to the excluded list in the session if not already present."""
    transaction_id = request.form.get('transaction_id')
    if transaction_id:
        if 'excluded_ids' not in session:
            session['excluded_ids'] = []

        current_excluded = session['excluded_ids']

        if transaction_id not in current_excluded:
            current_excluded.append(transaction_id)
            session['excluded_ids'] = current_excluded
            print(f"Excluded transaction ID: {transaction_id}")
            flash(f'Transaction {transaction_id[:8]}... excluded from summary.', 'warning')
        else:
            print(f"Transaction ID {transaction_id} already excluded.")
            flash(f'Transaction {transaction_id[:8]}... was already excluded.', 'info')
    else:
        flash('Could not exclude transaction: ID missing.', 'danger')

    # --- Preserve date range on redirect ---
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('index', start_date=start_date, end_date=end_date))


@app.route('/clear_exclusions', methods=['POST'])
def clear_exclusions():
    """Clears the list of excluded transaction IDs from the session."""
    if 'excluded_ids' in session and session['excluded_ids']:
        session.pop('excluded_ids')
        print("Cleared all excluded transaction IDs.")
        flash('All transaction exclusions cleared.', 'info')
    else:
        flash('No exclusions to clear.', 'info')

    # --- Preserve date range on redirect ---
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('index', start_date=start_date, end_date=end_date))

# --- Run the App ---
if __name__ == '__main__':
    if not app.secret_key or app.secret_key == "default-dev-secret-key-change-me-in-production":
         print("\n*** WARNING: Flask Secret Key is not securely set! ***")
         print("*** Session data may not be secure. Set FLASK_SECRET_KEY in your .env file. ***\n")
    print("Starting Flask development server...")
    app.run(debug=True, port=5001)