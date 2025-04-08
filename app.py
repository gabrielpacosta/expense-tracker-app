import os
import datetime
import json
import re
from collections import defaultdict
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
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")
PLAID_ACCESS_TOKEN = os.getenv("PLAID_ACCESS_TOKEN_PRIMARY")

# --- Helper Function to get dates ---
def get_date_range():
    """
    Determines the start and end dates.
    Default: Start is last Sunday, End is today.
    Overrides with query parameters 'start_date' and 'end_date' if valid.
    Returns (start_date_obj, end_date_obj, start_date_obj_display, end_date_obj_display)
    """
    today = datetime.datetime.now()
    offset = (today.weekday() + 1) % 7
    default_start_date = today - datetime.timedelta(days=offset)
    default_end_date = today

    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')

    start_date_obj = default_start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date_obj = default_end_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    valid_override = False
    temp_start = start_date_obj
    temp_end = end_date_obj

    try:
        if start_date_str:
            temp_start = datetime.datetime.strptime(start_date_str, '%Y-%m-%d')
            valid_override = True
        if end_date_str:
            temp_end = datetime.datetime.strptime(end_date_str, '%Y-%m-%d').replace(hour=23, minute=59, second=59, microsecond=999999)
            valid_override = True
        if valid_override:
            if temp_end < temp_start:
                 flash("End date cannot be before start date. Using default range.", "warning")
                 start_date_obj = default_start_date.replace(hour=0, minute=0, second=0, microsecond=0)
                 end_date_obj = default_end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
            else:
                 start_date_obj = temp_start
                 end_date_obj = temp_end
    except ValueError:
        flash("Invalid date format in URL. Please use YYYY-MM-DD. Using default range.", "warning")
        start_date_obj = default_start_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date_obj = default_end_date.replace(hour=23, minute=59, second=59, microsecond=999999)

    start_date_display = start_date_obj.date()
    end_date_display = end_date_obj.date()

    return start_date_obj, end_date_obj, start_date_display, end_date_display


# --- Plaid Client Setup ---
def initialize_plaid_client():
    """Initializes and returns the Plaid API client using environment URL strings."""
    try:
        environment_urls = { 'sandbox': 'https://sandbox.plaid.com', 'development': 'https://development.plaid.com', 'production': 'https://production.plaid.com', }
        plaid_env_key = PLAID_ENV.lower() if PLAID_ENV else 'sandbox'
        host_url = environment_urls.get(plaid_env_key)
        if host_url is None:
            valid_envs = ", ".join(environment_urls.keys())
            print(f"Error: Invalid PLAID_ENV value '{PLAID_ENV}' in .env file. Use one of: {valid_envs}")
            flash(f"Invalid Plaid environment configured: '{PLAID_ENV}'. Use one of: {valid_envs}", "danger")
            return None
        configuration = Configuration( host=host_url, api_key={ 'clientId': PLAID_CLIENT_ID, 'secret': PLAID_SECRET, } )
        if not PLAID_CLIENT_ID or not PLAID_SECRET:
             print("Error: PLAID_CLIENT_ID or PLAID_SECRET not found in .env file.")
             flash("Plaid Client ID or Secret missing in configuration. Check .env file.", "danger")
             return None
        api_client = ApiClient(configuration)
        client = plaid_api.PlaidApi(api_client)
        print(f"Plaid client initialized successfully for {plaid_env_key} environment.")
        return client
    except Exception as e:
        print(f"Error initializing Plaid client: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error initializing Plaid client. Check terminal logs.", "danger")
        return None

# --- Plaid Data Fetching Function ---
def get_plaid_transactions(client, access_token, start_date, end_date):
    """
    Fetches transactions from Plaid. Returns a list of formatted transactions
    using PLAID'S ORIGINAL AMOUNT SIGN convention (+ income, - expense).
    """
    if not client or not access_token:
        flash("Plaid client or access token is missing. Check .env configuration.", "danger")
        return None
    try:
        accounts_request = AccountsGetRequest(access_token=access_token)
        accounts_response = client.accounts_get(accounts_request)
        account_map = {acc.account_id: acc.name for acc in accounts_response['accounts']}
        print(f"Account map: {account_map}")

        request = TransactionsGetRequest( access_token=access_token, start_date=start_date.date(), end_date=end_date.date(), options=TransactionsGetRequestOptions( count=500, offset=0 ) )
        response = client.transactions_get(request)
        transactions_result = response['transactions']

        while len(transactions_result) < response['total_transactions']:
             request.options.offset = len(transactions_result)
             response = client.transactions_get(request)
             transactions_result.extend(response['transactions'])

        print(f"Fetched {len(transactions_result)} transactions from Plaid.")

        formatted_transactions = []
        for t in transactions_result:
            if t['pending']: continue
            category_list = t['category'] if t['category'] else ['Uncategorized']
            category_str = ' > '.join(category_list)
            account_name = account_map.get(t['account_id'], 'Unknown Account')
            amount = t['amount'] # KEEP ORIGINAL SIGN (+income, -expense)
            formatted_transactions.append({
                'id': t['transaction_id'], 'date': t['date'], 'account': account_name,
                'name': t['name'], 'amount': amount, 'category': category_str,
                'category_list': category_list
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


# --- Function to find and exclude offsetting transfers ---
def find_and_exclude_offsetting_transfers(transactions, days_window=2):
    """
    Identifies potential internal transfers that offset each other (sum to zero)
    within a specified time window. Returns a set of transaction IDs to exclude.
    """
    excluded_ids = set()
    amount_groups = defaultdict(list)
    for t in transactions:
        # Customize this logic: Identify potential transfers
        is_potential_transfer = False
        if t['category_list'] and t['category_list'][0].lower() == 'transfer':
            is_potential_transfer = True
        # Add name checks if needed
        name_lower = t['name'].lower()
        if 'online transfer' in name_lower or 'transfer from' in name_lower or 'transfer to' in name_lower:
            is_potential_transfer = True

        if is_potential_transfer:
             amount_groups[abs(t['amount'])].append(t) # Group by absolute amount

    print(f"Found {len(amount_groups)} potential transfer amount groups.")

    processed_ids = set()
    for amount, group in amount_groups.items():
        if amount == 0: continue
        deposits = [t for t in group if t['amount'] > 0] # Plaid income is positive
        withdrawals = [t for t in group if t['amount'] < 0] # Plaid expense is negative

        for deposit in deposits:
            if deposit['id'] in processed_ids: continue
            for withdrawal in withdrawals:
                if withdrawal['id'] in processed_ids: continue

                if abs(deposit['amount'] + withdrawal['amount']) < 0.01: # Sum is close to 0
                    date1 = deposit['date']
                    date2 = withdrawal['date']
                    time_difference = abs(date1 - date2)

                    if time_difference <= datetime.timedelta(days=days_window):
                        print(f"Found offsetting pair: +{deposit['amount']:.2f} ({deposit['name']}/{deposit['date']}) and {withdrawal['amount']:.2f} ({withdrawal['name']}/{withdrawal['date']})")
                        excluded_ids.add(deposit['id'])
                        excluded_ids.add(withdrawal['id'])
                        processed_ids.add(deposit['id'])
                        processed_ids.add(withdrawal['id'])
                        break # Move to next deposit

    print(f"Auto-excluding {len(excluded_ids)} offsetting transfer transactions.")
    return excluded_ids


# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main page, fetching and displaying transactions."""
    print("Route requested: / (index)")

    if 'excluded_ids' not in session: session['excluded_ids'] = []
    user_excluded_ids = session['excluded_ids'] # Manual list

    start_date_obj, end_date_obj, start_date_display, end_date_display = get_date_range()

    plaid_client = initialize_plaid_client()
    transactions = []
    total_income = 0.0
    total_expenses = 0.0
    balance = 0.0
    combined_excluded_ids = set(user_excluded_ids) # Start with user exclusions

    if plaid_client and PLAID_ACCESS_TOKEN:
        print(f"Fetching Plaid transactions from {start_date_obj.strftime('%Y-%m-%d')} to {end_date_obj.strftime('%Y-%m-%d')}")
        transactions_data = get_plaid_transactions(
            plaid_client,
            PLAID_ACCESS_TOKEN,
            start_date_obj,
            end_date_obj
        )
        if transactions_data is not None:
            transactions = transactions_data
            auto_excluded_ids = find_and_exclude_offsetting_transfers(transactions)
            combined_excluded_ids.update(auto_excluded_ids) # Add auto-excluded

            # --- Calculate totals ---
            for t in transactions:
                # print(f"Processing: {t['date']} | {t['name']} | Amount: {t['amount']}") # DEBUG LINE
                if t['id'] in combined_excluded_ids:
                    # print(f"  -> Skipping (Excluded)") # DEBUG LINE
                    continue

                # Plaid: amount > 0 is income, amount < 0 is expense
                if t['amount'] > 0:
                    # print(f"  -> Adding {t['amount']} to income") # DEBUG LINE
                    total_income += t['amount']
                elif t['amount'] < 0:
                    # print(f"  -> Adding {abs(t['amount'])} to expenses") # DEBUG LINE
                    total_expenses += abs(t['amount'])

            balance = total_income - total_expenses
            # print(f"Final Totals: Income={total_income:.2f}, Expenses={total_expenses:.2f}, Balance={balance:.2f}") # DEBUG LINE

    elif not PLAID_ACCESS_TOKEN:
         flash("PLAID_ACCESS_TOKEN_PRIMARY not found in .env file.", "danger")
    else:
        pass # Error flashed in client init

    return render_template(
        'index.html',
        transactions=transactions,
        total_income=total_income,
        total_expenses=total_expenses,
        balance=balance,
        start_date_value=start_date_display.strftime('%Y-%m-%d'),
        end_date_value=end_date_display.strftime('%Y-%m-%d'),
        combined_excluded_ids=combined_excluded_ids,
        user_excluded_count=len(user_excluded_ids)
        )


@app.route('/refresh', methods=['POST'])
def trigger_refresh():
    # (No changes needed here)
    print("Route requested: /refresh (POST)")
    flash('Refreshing transaction data...', 'info')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('index', start_date=start_date, end_date=end_date))


@app.route('/exclude', methods=['POST'])
def exclude_transaction():
    # (No changes needed here)
    transaction_id = request.form.get('transaction_id')
    if transaction_id:
        if 'excluded_ids' not in session: session['excluded_ids'] = []
        current_user_excluded = session['excluded_ids']
        if transaction_id not in current_user_excluded:
            current_user_excluded.append(transaction_id)
            session['excluded_ids'] = current_user_excluded
            print(f"User excluded transaction ID: {transaction_id}")
            flash(f'Transaction {transaction_id[:8]}... manually excluded from summary.', 'warning')
        else:
            print(f"Transaction ID {transaction_id} already user-excluded.")
            flash(f'Transaction {transaction_id[:8]}... was already manually excluded.', 'info')
    else: flash('Could not exclude transaction: ID missing.', 'danger')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('index', start_date=start_date, end_date=end_date))


@app.route('/clear_exclusions', methods=['POST'])
def clear_exclusions():
    # (No changes needed here)
    if 'excluded_ids' in session and session['excluded_ids']:
        session.pop('excluded_ids')
        print("Cleared USER excluded transaction IDs.")
        flash('Manually excluded transactions reset.', 'info')
    else: flash('No manual exclusions to clear.', 'info')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    return redirect(url_for('index', start_date=start_date, end_date=end_date))


# --- Run the App ---
if __name__ == '__main__':
    if not app.secret_key or app.secret_key == "default-dev-secret-key-change-me-in-production":
         print("\n*** WARNING: Flask Secret Key is not securely set! ***")
         print("*** Session data may not be secure. Set FLASK_SECRET_KEY in your .env file. ***\n")
    print("Starting Flask development server...")
    # Set debug=False when deploying to Render
    app.run(debug=False, port=5001) # Use debug=False for production/deployment