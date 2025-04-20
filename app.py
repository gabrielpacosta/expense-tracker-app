import os
import datetime
import json
import re
from collections import defaultdict
# dateutil might not be installed by default - Add to requirements.txt if needed!
# pip install python-dateutil
# from dateutil.relativedelta import relativedelta
from flask import Flask, render_template, redirect, url_for, flash, request, session
from dotenv import load_dotenv

# --- Plaid Client Libraries ---
# (Imports remain the same)
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
# (Initialization remains the same)
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key:
    print("CRITICAL: FLASK_SECRET_KEY not set in .env. Session persistence will not work reliably.")
    app.secret_key = "default-insecure-key-set-in-environment"

# --- Plaid Configuration ---
# (Configuration remains the same)
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")
PLAID_ACCESS_TOKEN = os.getenv("PLAID_ACCESS_TOKEN_PRIMARY")

# --- Helper Function to get TARGET WEEK dates ---
# (get_target_week_dates function remains the same)
def get_target_week_dates():
    today_date = datetime.date.today()
    current_week_monday_offset = today_date.weekday() # Monday is 0
    current_week_monday = today_date - datetime.timedelta(days=current_week_monday_offset)
    target_week_start_str = request.args.get('week_start')
    target_week_start = current_week_monday # Default
    if target_week_start_str:
        try:
            parsed_date = datetime.datetime.strptime(target_week_start_str, '%Y-%m-%d').date()
            parsed_offset = parsed_date.weekday()
            target_week_start = parsed_date - datetime.timedelta(days=parsed_offset)
        except ValueError:
            flash("Invalid week_start date format in URL. Using current week.", "warning")
            target_week_start = current_week_monday
    return target_week_start


# --- Plaid Client Setup ---
# (initialize_plaid_client function remains the same)
def initialize_plaid_client():
    try:
        environment_urls = { 'sandbox': 'https://sandbox.plaid.com', 'development': 'https://development.plaid.com', 'production': 'https://production.plaid.com', }
        plaid_env_key = PLAID_ENV.lower() if PLAID_ENV else 'sandbox'
        host_url = environment_urls.get(plaid_env_key)
        if host_url is None:
            valid_envs = ", ".join(environment_urls.keys()); print(f"Error: Invalid PLAID_ENV value '{PLAID_ENV}' in .env file. Use one of: {valid_envs}"); flash(f"Invalid Plaid environment configured: '{PLAID_ENV}'. Use one of: {valid_envs}", "danger"); return None
        configuration = Configuration( host=host_url, api_key={ 'clientId': PLAID_CLIENT_ID, 'secret': PLAID_SECRET, } )
        if not PLAID_CLIENT_ID or not PLAID_SECRET: print("Error: PLAID_CLIENT_ID or PLAID_SECRET not found in .env file."); flash("Plaid Client ID or Secret missing in configuration. Check .env file.", "danger"); return None
        api_client = ApiClient(configuration); client = plaid_api.PlaidApi(api_client)
        print(f"Plaid client initialized successfully for {plaid_env_key} environment.")
        return client
    except Exception as e: print(f"Error initializing Plaid client: {e}"); import traceback; traceback.print_exc(); flash(f"Error initializing Plaid client. Check terminal logs.", "danger"); return None

# --- Plaid Data Fetching Function ---
# (get_plaid_transactions function remains the same - returns original amounts)
def get_plaid_transactions(client, access_token, start_date, end_date):
    """ Fetches transactions, expects date objects for start/end """
    if not client or not access_token: flash("Plaid client or access token is missing. Check .env configuration.", "danger"); return None
    try:
        accounts_request = AccountsGetRequest(access_token=access_token)
        accounts_response = client.accounts_get(accounts_request)
        account_map = {acc.account_id: acc.name for acc in accounts_response['accounts']}
        print(f"Account map: {account_map}")
        request = TransactionsGetRequest( access_token=access_token, start_date=start_date, end_date=end_date, options=TransactionsGetRequestOptions( count=500, offset=0 ) )
        response = client.transactions_get(request)
        transactions_result = response['transactions']
        while len(transactions_result) < response['total_transactions']:
             request.options.offset = len(transactions_result)
             response = client.transactions_get(request)
             transactions_result.extend(response['transactions'])
        print(f"Fetched {len(transactions_result)} transactions from Plaid for range {start_date} to {end_date}.")
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
                'category_list': category_list # Keep original list for checking
            })
        formatted_transactions.sort(key=lambda x: x['date'], reverse=True)
        return formatted_transactions
    except plaid.ApiException as e:
        error_response = json.loads(e.body); error_message = error_response.get('error_message', 'Unknown Plaid API error'); error_code = error_response.get('error_code', 'UNKNOWN')
        print(f"Plaid API Error: {error_code} - {error_message}"); flash(f"Error fetching data from Plaid: {error_message} ({error_code})", "danger")
        if error_code == 'ITEM_LOGIN_REQUIRED': flash("Bank connection needs update. Re-link account required.", "warning")
        return None
    except Exception as e:
        print(f"An unexpected error occurred fetching Plaid data: {e}"); import traceback; traceback.print_exc()
        flash(f"An unexpected error occurred: {e}", "danger")
        return None

# --- Function to find and exclude offsetting transfers ---
# (find_and_exclude_offsetting_transfers function remains the same)
def find_and_exclude_offsetting_transfers(transactions, days_window=2):
    excluded_ids = set(); amount_groups = defaultdict(list)
    for t in transactions:
        is_potential_transfer = False
        # Refined check: Look for 'Transfer' as the primary category
        if t['category_list'] and t['category_list'][0].lower() == 'transfer': is_potential_transfer = True
        # Keep name checks as backup/alternative
        name_lower = t['name'].lower()
        if 'online transfer' in name_lower or 'transfer from' in name_lower or 'transfer to' in name_lower: is_potential_transfer = True

        if is_potential_transfer: amount_groups[abs(t['amount'])].append(t)
    print(f"Found {len(amount_groups)} potential transfer amount groups.")
    processed_ids = set()
    for amount, group in amount_groups.items():
        if amount == 0: continue
        deposits = [t for t in group if t['amount'] > 0]; withdrawals = [t for t in group if t['amount'] < 0]
        for deposit in deposits:
            if deposit['id'] in processed_ids: continue
            for withdrawal in withdrawals:
                if withdrawal['id'] in processed_ids: continue
                if abs(deposit['amount'] + withdrawal['amount']) < 0.01:
                    date1 = deposit['date']; date2 = withdrawal['date']; time_difference = abs(date1 - date2)
                    if time_difference <= datetime.timedelta(days=days_window):
                        print(f"Found offsetting pair: +{deposit['amount']:.2f} ({deposit['name']}/{deposit['date']}) and {withdrawal['amount']:.2f} ({withdrawal['name']}/{withdrawal['date']})")
                        excluded_ids.add(deposit['id']); excluded_ids.add(withdrawal['id'])
                        processed_ids.add(deposit['id']); processed_ids.add(withdrawal['id'])
                        break
    print(f"Auto-excluding {len(excluded_ids)} offsetting transfer transactions.")
    return excluded_ids

# --- Function to identify Rent transactions ---
def is_rent_transaction(transaction):
    """ Checks if a transaction is likely rent. Customize logic as needed. """
    # --- !! CUSTOMIZE THIS SECTION !! ---
    # Option 1: Check category hierarchy (more specific)
    # Example: Plaid's detailed category for rent
    rent_category_path = ["service", "financial", "rent and mortgage"]
    # Check if the transaction's category list matches the start of the rent path
    if transaction['category_list'] and len(transaction['category_list']) >= len(rent_category_path):
         if all(c.lower() == rent_category_path[i] for i, c in enumerate(transaction['category_list'][:len(rent_category_path)])):
             print(f"Identified as Rent by category: {transaction['name']} | {transaction['category']}")
             return True

    # Option 2: Check simpler category terms (broader)
    if 'rent' in transaction['category'].lower():
        print(f"Identified as Rent by category keyword: {transaction['name']} | {transaction['category']}")
        return True

    # Option 3: Check transaction name (less reliable, use specific patterns)
    name_lower = transaction['name'].lower()
    if 'rent payment' in name_lower or 'mthly rent' in name_lower: # Add your landlord's name or specific memo text
        print(f"Identified as Rent by name: {transaction['name']}")
        return True
    # --- End Customization ---

    return False

# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main page, fetching and displaying transactions."""
    print("Route requested: / (index)")

    if 'user_excluded_ids' not in session: session['user_excluded_ids'] = []
    user_excluded_ids = session['user_excluded_ids']

    # Determine Target Week Dates & MTD Dates
    today_date = datetime.date.today()
    target_week_start = get_target_week_dates()
    target_week_end = target_week_start + datetime.timedelta(days=6)
    prev_week_start = target_week_start - datetime.timedelta(days=7)
    next_week_start = target_week_start + datetime.timedelta(days=7)
    current_week_monday_offset = today_date.weekday()
    current_week_monday = today_date - datetime.timedelta(days=current_week_monday_offset)
    is_current_week = (target_week_start == current_week_monday)
    month_start_date = today_date.replace(day=1)
    month_end_date = today_date

    plaid_client = initialize_plaid_client()
    all_mtd_transactions = []
    transactions_to_display = []
    total_income_mtd = 0.0; total_expenses_mtd = 0.0; balance_mtd = 0.0
    total_income_week = 0.0; total_expenses_week = 0.0; balance_week = 0.0
    rent_total_mtd = 0.0 # <--- Initialize Rent Total for MTD
    excluded_in_target_week_count = 0
    combined_excluded_ids = set(user_excluded_ids)

    if plaid_client and PLAID_ACCESS_TOKEN:
        print(f"Fetching ALL Plaid transactions from {month_start_date} to {month_end_date}")
        all_mtd_transactions = get_plaid_transactions(
            plaid_client, PLAID_ACCESS_TOKEN, month_start_date, month_end_date
        )

        if all_mtd_transactions is not None:
            auto_excluded_ids = find_and_exclude_offsetting_transfers(all_mtd_transactions)
            combined_excluded_ids.update(auto_excluded_ids)

            transactions_to_display = [t for t in all_mtd_transactions if target_week_start <= t['date'] <= target_week_end]

            # --- Calculate Summaries ---
            for t in all_mtd_transactions:
                is_excluded = (t['id'] in combined_excluded_ids)
                is_in_target_week = (target_week_start <= t['date'] <= target_week_end)

                # Count exclusions visible in the target week
                if is_excluded and is_in_target_week:
                     excluded_in_target_week_count += 1

                # Skip excluded items for financial calculations
                if is_excluded:
                    continue

                # Identify rent transactions (only need to do this once per transaction)
                is_rent = is_rent_transaction(t)

                # Calculate MTD totals (using inverted logic as requested)
                if t['amount'] < 0: # Income
                    total_income_mtd += abs(t['amount'])
                elif t['amount'] > 0: # Expense
                    current_expense = t['amount']
                    total_expenses_mtd += current_expense
                    # Add to rent total if it's a rent expense
                    if is_rent:
                        rent_total_mtd += current_expense

                # Calculate Weekly totals (if in target week)
                if is_in_target_week:
                    if t['amount'] < 0: # Income
                        total_income_week += abs(t['amount'])
                    elif t['amount'] > 0: # Expense
                        total_expenses_week += t['amount']

            balance_mtd = total_income_mtd - total_expenses_mtd
            balance_week = total_income_week - total_expenses_week
            # Calculate MTD expenses without rent
            total_expenses_mtd_without_rent = total_expenses_mtd - rent_total_mtd

    elif not PLAID_ACCESS_TOKEN: flash("PLAID_ACCESS_TOKEN_PRIMARY not found in .env file.", "danger")
    else: pass

    return render_template(
        'index.html',
        transactions=transactions_to_display,
        # MTD Summary Data
        total_income_mtd=total_income_mtd,
        total_expenses_mtd=total_expenses_mtd,
        total_expenses_mtd_without_rent=total_expenses_mtd_without_rent, # <-- Pass new value
        balance_mtd=balance_mtd,
        month_start_date=month_start_date,
        month_end_date=month_end_date,
        # Target Week Summary Data
        total_income_week=total_income_week,
        total_expenses_week=total_expenses_week,
        balance_week=balance_week,
        target_week_start=target_week_start,
        target_week_end=target_week_end,
        # Navigation Data
        prev_week_start_str=prev_week_start.strftime('%Y-%m-%d'),
        next_week_start_str=next_week_start.strftime('%Y-%m-%d'),
        is_current_week=is_current_week,
        # Exclusion Data
        combined_excluded_ids=combined_excluded_ids,
        user_excluded_ids=user_excluded_ids,
        user_excluded_count=len(user_excluded_ids),
        excluded_in_target_week_count=excluded_in_target_week_count
        )

# --- Other Routes (Refresh, Exclude, Clear) ---
# (Remain the same, pass 'week_start')
@app.route('/refresh', methods=['POST'])
def trigger_refresh():
    print("Route requested: /refresh (POST)")
    flash('Refreshing transaction data...', 'info')
    week_start = request.args.get('week_start'); return redirect(url_for('index', week_start=week_start))

@app.route('/exclude', methods=['POST'])
def exclude_transaction():
    transaction_id = request.form.get('transaction_id')
    if transaction_id:
        if 'user_excluded_ids' not in session: session['user_excluded_ids'] = []
        current_user_excluded = session['user_excluded_ids']
        if transaction_id not in current_user_excluded:
            current_user_excluded.append(transaction_id); session['user_excluded_ids'] = current_user_excluded
            print(f"User excluded transaction ID: {transaction_id}"); flash(f'Transaction {transaction_id[:8]}... manually excluded.', 'warning')
        else: flash(f'Transaction {transaction_id[:8]}... was already manually excluded.', 'info')
    else: flash('Could not exclude transaction: ID missing.', 'danger')
    week_start = request.args.get('week_start'); return redirect(url_for('index', week_start=week_start))

@app.route('/include', methods=['POST'])
def include_transaction():
    transaction_id = request.form.get('transaction_id')
    if transaction_id:
        if 'user_excluded_ids' in session:
            current_user_excluded = session['user_excluded_ids']
            if transaction_id in current_user_excluded:
                current_user_excluded.remove(transaction_id); session['user_excluded_ids'] = current_user_excluded
                print(f"User included transaction ID: {transaction_id}"); flash(f'Transaction {transaction_id[:8]}... re-included.', 'info')
            else: flash(f'Transaction {transaction_id[:8]}... was not manually excluded.', 'warning')
        else: flash('No manual exclusions list found.', 'warning')
    else: flash('Could not include transaction: ID missing.', 'danger')
    week_start = request.args.get('week_start'); return redirect(url_for('index', week_start=week_start))

@app.route('/clear_exclusions', methods=['POST'])
def clear_exclusions():
    if 'user_excluded_ids' in session and session['user_excluded_ids']:
        session.pop('user_excluded_ids'); print("Cleared USER excluded transaction IDs.")
        flash('Manually excluded transactions reset.', 'info')
    else: flash('No manual exclusions to clear.', 'info')
    week_start = request.args.get('week_start'); return redirect(url_for('index', week_start=week_start))

# --- Run the App ---
if __name__ == '__main__':
    if not app.secret_key or app.secret_key == "default-insecure-key-set-in-environment": print("\n*** WARNING: Flask Secret Key is not securely set! ***\n*** Session data will not persist reliably between browser restarts. Set FLASK_SECRET_KEY in your .env file. ***\n")
    print("Starting Flask development server...")
    app.run(debug=False, port=5001) # Use debug=False for deployment