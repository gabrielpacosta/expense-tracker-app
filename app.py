import os
import datetime
import json
import re
import logging
import traceback
from collections import defaultdict
# dateutil is NOT strictly needed for the current date calculations (weeks, MTD)
# datetime.timedelta is sufficient. Remove if not planning more advanced date math.
# from dateutil.relativedelta import relativedelta

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

# --- Setup Logging ---
# Moved logging setup to the top for clarity and accessibility
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[logging.FileHandler("app.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__) # <-- logger defined at module level

logger.info("Application starting...")

# --- Load Environment Variables ---
load_dotenv()
logger.info("Environment variables loaded")

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key or app.secret_key == "default-insecure-key-set-in-environment":
    logger.critical("FLASK_SECRET_KEY not set in .env. Session persistence will not work reliably. Using insecure default.")
    app.secret_key = "default-insecure-key-set-in-environment" # Using a more random default is slightly better, but requires secrets module

# Generate a more secure default key if needed
# import secrets
# if not app.secret_key or app.secret_key == "default-insecure-key-set-in-environment":
#     logger.critical("FLASK_SECRET_KEY not set in .env. Session persistence will not work reliably. Generating a temporary key.")
#     app.secret_key = secrets.token_hex(16) # Use a random key for the current run

# --- Plaid Configuration ---
PLAID_CLIENT_ID = os.getenv("PLAID_CLIENT_ID")
PLAID_SECRET = os.getenv("PLAID_SECRET")
PLAID_ENV = os.getenv("PLAID_ENV", "sandbox")
# PLAID_ACCESS_TOKEN_PRIMARY is used here for simplicity in this quickstart
# For a real multi-user app, you'd store and retrieve access tokens per user/item.
PLAID_ACCESS_TOKEN = os.getenv("PLAID_ACCESS_TOKEN_PRIMARY")

logger.info(f"Plaid configuration: ENV={PLAID_ENV}, CLIENT_ID={'SET' if PLAID_CLIENT_ID else 'NOT SET'}, "
            f"SECRET={'SET' if PLAID_SECRET else 'NOT SET'}, "
            f"ACCESS_TOKEN={'SET' if PLAID_ACCESS_TOKEN else 'NOT SET'}")

# --- Constants ---
TRANSFER_DETECTION_WINDOW_DAYS = 2 # Days window for finding matching transfers

# --- Helper Function to get TARGET WEEK dates ---
def get_target_week_dates():
    """
    Determines the start date of the target week based on the 'week_start'
    URL parameter or defaults to the start of the current week.
    """
    global logger # Ensure logger is accessible in this function
    try:
        today_date = datetime.date.today()
        # Calculate the Monday of the current week (weekday() returns 0 for Monday)
        current_week_monday = today_date - datetime.timedelta(days=today_date.weekday())

        target_week_start_str = request.args.get('week_start')
        target_week_start = current_week_monday  # Default

        if target_week_start_str:
            try:
                parsed_date = datetime.datetime.strptime(target_week_start_str, '%Y-%m-%d').date()
                # Ensure the parsed date is also a Monday
                parsed_offset = parsed_date.weekday()
                target_week_start = parsed_date - datetime.timedelta(days=parsed_offset)
                logger.debug(f"Using week_start from URL: {target_week_start}")
            except ValueError:
                logger.warning(f"Invalid week_start date format '{target_week_start_str}' in URL. Using current week.")
                flash("Invalid week_start date format in URL. Using current week.", "warning")
                target_week_start = current_week_monday
        else:
            logger.debug(f"No week_start in URL. Using current week: {target_week_start}")

        return target_week_start
    except Exception as e:
        logger.error(f"Error in get_target_week_dates: {str(e)}", exc_info=True) # Add exc_info
        traceback.print_exc()
        # Return current Monday as a fallback
        today = datetime.date.today()
        return today - datetime.timedelta(days=today.weekday())

# --- Plaid Client Setup ---
def initialize_plaid_client():
    """Initializes and returns a Plaid API client."""
    global logger # Ensure logger is accessible in this function
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
            logger.error(f"Invalid PLAID_ENV value '{PLAID_ENV}' in .env file. Use one of: {valid_envs}")
            flash(f"Invalid Plaid environment configured: '{PLAID_ENV}'. Use one of: {valid_envs}", "danger")
            return None

        if not PLAID_CLIENT_ID or not PLAID_SECRET:
            logger.error("PLAID_CLIENT_ID or PLAID_SECRET not found in .env file.")
            flash("Plaid Client ID or Secret missing in configuration. Check .env file.", "danger")
            return None

        configuration = Configuration(
            host=host_url,
            api_key={
                'clientId': PLAID_CLIENT_ID,
                'secret': PLAID_SECRET,
            }
        )

        api_client = ApiClient(configuration)
        client = plaid_api.PlaidApi(api_client)
        logger.info(f"Plaid client initialized successfully for {plaid_env_key} environment.")
        return client
    except Exception as e:
        logger.error(f"Error initializing Plaid client: {e}", exc_info=True) # Add exc_info
        traceback.print_exc()
        flash(f"Error initializing Plaid client: {str(e)}", "danger")
        return None

# --- Plaid Data Fetching Function ---
def get_plaid_transactions(client, access_token, start_date, end_date):
    """
    Fetches transactions from Plaid for a given date range and access token.
    Expects start_date and end_date as datetime.date objects.
    Returns a list of formatted transaction dictionaries or None on error.
    """
    global logger # Ensure logger is accessible in this function
    if not client or not access_token:
        logger.error("Plaid client or access token is missing")
        # Flashing handled by caller or init function
        return None

    try:
        # Fetch accounts first to map account_ids to names
        logger.debug(f"Fetching accounts for access token: {access_token[:8]}...")
        accounts_request = AccountsGetRequest(access_token=access_token)
        accounts_response = client.accounts_get(accounts_request)
        # Ensure 'accounts' key exists in response, although it typically does
        account_map = {acc.account_id: acc.name for acc in accounts_response.get('accounts', [])}
        logger.debug(f"Retrieved {len(account_map)} accounts")

        # Now fetch transactions
        logger.debug(f"Fetching transactions from {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        request = TransactionsGetRequest(
            access_token=access_token,
            start_date=start_date,
            end_date=end_date,
            options=TransactionsGetRequestOptions(
                # Max count per page is 500
                count=500,
                offset=0
            )
        )

        response = client.transactions_get(request)
        transactions_result = response.get('transactions', []) # Use .get for safety
        total_transactions = response.get('total_transactions', len(transactions_result)) # Use .get for safety

        # Paginate if there are more transactions
        while len(transactions_result) < total_transactions:
            logger.debug(f"Paginating transactions: fetched {len(transactions_result)} of estimated {total_transactions}")
            request.options.offset = len(transactions_result)
            # Add a small buffer/check to prevent infinite loops on bad total_transactions
            if total_transactions > 0 and request.options.offset >= total_transactions + 500:
                 logger.warning(f"Offset {request.options.offset} exceeding estimated total {total_transactions} significantly. Stopping pagination.")
                 break
            response = client.transactions_get(request)
            new_transactions = response.get('transactions', [])
            if not new_transactions:
                logger.warning("Pagination returned empty list, stopping.")
                break
            transactions_result.extend(new_transactions)


        logger.info(f"Fetched {len(transactions_result)} transactions from Plaid for range {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}.")

        # Format transactions for display and calculation
        formatted_transactions = []
        for t in transactions_result:
            # Skip pending transactions as they are not final
            if t.get('pending', False): # Use .get for safety
                continue

            # Use .get for safety, provide default empty list
            category_list = t.get('category')
            if not category_list: # Handle None or empty list
                 category_list = ['Uncategorized']
            category_str = ' > '.join(category_list)

            # Use .get for safety
            account_name = account_map.get(t.get('account_id'), 'Unknown Account')

            # Plaid's amount is positive for debits (expenses), negative for credits (income)
            # Use .get - might be None if data is weird
            amount = t.get('amount')

            # Basic validation for critical fields
            if amount is None or t.get('transaction_id') is None or t.get('date') is None or t.get('name') is None:
                 logger.warning(f"Skipping transaction with missing critical data: ID={t.get('transaction_id')}, Name={t.get('name')}, Date={t.get('date')}")
                 continue

            # The Plaid Python client already converts the date string to a datetime.date object
            # Access t['date'] directly, it's already the desired type.
            transaction_date = t['date'] # This is already a datetime.date object

            # Defensive check: ensure it really is a date object if needed
            if not isinstance(transaction_date, datetime.date):
                 logger.warning(f"Skipping transaction {t.get('transaction_id')} - {t.get('name')} because its date is not a datetime.date object (Type: {type(transaction_date)}): {transaction_date}")
                 continue

            formatted_transactions.append({
                'id': t['transaction_id'],
                'date': transaction_date, # Use the date object provided by the Plaid client
                'account': account_name,
                'name': t['name'],
                'amount': amount, # Keep original Plaid sign
                'category': category_str,
                'category_list': category_list  # Keep original list for checking hierarchy
            })

        # Transactions might already be sorted by date from Plaid, but ensure they are
        # Sort transactions by date, newest first
        formatted_transactions.sort(key=lambda x: x['date'], reverse=True)
        return formatted_transactions

    except plaid.ApiException as e:
        # Log detailed Plaid API error
        try:
            error_response = json.loads(e.body)
            error_message = error_response.get('error_message', 'Unknown Plaid API error')
            error_code = error_response.get('error_code', 'UNKNOWN')
            logger.error(f"Plaid API Error: {error_code} - {error_message}", exc_info=True)
            flash(f"Error fetching data from Plaid: {error_message} ({error_code})", "danger")

            if error_code == 'ITEM_LOGIN_REQUIRED':
                flash("Bank connection needs update. Re-link account required.", "warning")

        except (json.JSONDecodeError, KeyError) as json_e:
             logger.error(f"Plaid API Error (could not parse response body): {e}", exc_info=True)
             flash(f"Error fetching data from Plaid (API error, could not parse response): {str(e)}", "danger")

        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching Plaid data: {e}", exc_info=True)
        traceback.print_exc()
        flash(f"An unexpected error occurred while fetching Plaid data: {str(e)}", "danger")
        return None

# --- Function to find and exclude offsetting transfers ---
def find_and_exclude_offsetting_transfers(transactions, days_window=TRANSFER_DETECTION_WINDOW_DAYS):
    """
    Identifies potential inter-account transfers by looking for matching
    positive and negative amounts within a specified date window.
    Expects a list of formatted transaction dictionaries.
    Returns a set of transaction IDs to exclude.
    """
    global logger # Ensure logger is accessible in this function
    try:
        excluded_ids = set()
        # Group transactions by absolute amount for efficient comparison
        amount_groups = defaultdict(list)

        for t in transactions:
            # Use .get for safety
            category_list = t.get('category_list', [])
            name_lower = t.get('name', '').lower()
            amount = t.get('amount')
            transaction_id = t.get('id')

            # Basic validation
            if amount is None or transaction_id is None or t.get('date') is None:
                 logger.debug(f"Skipping transaction {transaction_id} in transfer detection due to missing data.")
                 continue

            is_potential_transfer = False

            # Refined check: Look for 'Transfer' as the primary category or in specific category paths
            # Example: Plaid's standard "Transfer" category path
            transfer_category_path = ["transfer"]
            # Another common one might involve "Payroll" > "Direct Deposit" for incoming transfers from payroll
            # or "Service" > "Financial" > "Wire Transfer" etc.
            # Adjust this logic based on your specific transfer categories.
            if category_list and len(category_list) >= len(transfer_category_path) and \
               all(c.lower() == transfer_category_path[i] for i, c in enumerate(category_list[:len(transfer_category_path)])):
                is_potential_transfer = True

            # Keep name checks as backup/alternative - make them more specific if possible
            if not is_potential_transfer and ('online transfer' in name_lower or 'transfer from' in name_lower or 'transfer to' in name_lower):
                is_potential_transfer = True

            if is_potential_transfer:
                amount_groups[abs(amount)].append(t) # Group by absolute amount

        logger.debug(f"Found {len(amount_groups)} potential transfer amount groups.")
        processed_ids = set() # Keep track of IDs already matched in a pair

        for amount, group in amount_groups.items():
            if amount == 0: # Skip 0 amount transactions
                continue

            deposits = [t for t in group if t['amount'] < 0] # Plaid amount: Income is negative
            withdrawals = [t for t in group if t['amount'] > 0] # Plaid amount: Expense is positive

            for deposit in deposits:
                if deposit['id'] in processed_ids:
                    continue

                for withdrawal in withdrawals:
                    if withdrawal['id'] in processed_ids:
                        continue

                    # Check if amounts match (within a small tolerance for floating point)
                    if abs(deposit['amount'] + withdrawal['amount']) < 0.01: # Check sum is near zero
                        date1 = deposit['date']
                        date2 = withdrawal['date']

                        # Ensure date objects are comparable
                        if isinstance(date1, datetime.date) and isinstance(date2, datetime.date):
                            # Calculate difference in days
                            time_difference_days = abs((date1 - date2).days)
                            if time_difference_days <= days_window:
                                logger.debug(f"Found offsetting pair: Income ID={deposit['id'][:8]}... Amount={deposit['amount']:.2f} "
                                             f"({deposit.get('name', 'N/A')}/{deposit.get('date', 'N/A')}) and Expense ID={withdrawal['id'][:8]}... Amount={withdrawal['amount']:.2f} "
                                             f"({withdrawal.get('name', 'N/A')}/{withdrawal.get('date', 'N/A')}) within {time_difference_days} days.")
                                excluded_ids.add(deposit['id'])
                                excluded_ids.add(withdrawal['id'])
                                processed_ids.add(deposit['id'])
                                processed_ids.add(withdrawal['id'])
                                break # Move to the next deposit after finding a match for this withdrawal
                        else:
                             logger.warning(f"Skipping date comparison for potential transfer pair {deposit.get('id')} / {withdrawal.get('id')} due to invalid date format or type.")


        logger.info(f"Auto-excluding {len(excluded_ids)} offsetting transfer transactions.")
        return excluded_ids

    except Exception as e:
        logger.error(f"Error in find_and_exclude_offsetting_transfers: {str(e)}", exc_info=True) # Add exc_info
        traceback.print_exc()
        return set()  # Return empty set as fallback

# --- Function to identify Rent transactions ---
def is_rent_transaction(transaction):
    """
    Checks if a transaction is likely rent. Customize logic as needed.
    Expects a formatted transaction dictionary.
    Returns True if likely rent, False otherwise.
    """
    global logger # Ensure logger is accessible in this function
    try:
        # Use .get for safety
        category_list = transaction.get('category_list', [])
        name_lower = transaction.get('name', '').lower()
        amount = transaction.get('amount')
        transaction_id = transaction.get('id', 'N/A') # Use ID for logging

        # Ensure it's an expense (amount > 0 in Plaid's system) and not 0 or None
        if amount is None or amount <= 0:
            return False

        # --- !! CUSTOMIZE THIS SECTION !! ---
        # Option 1: Check category hierarchy (more specific)
        # Example: Plaid's standard "Service" > "Financial" > "Rent and mortgage"
        rent_category_path_1 = ["service", "financial", "rent and mortgage"]
        # Another possible path might just be "Housing" > "Rent" depending on aggregation
        rent_category_path_2 = ["housing", "rent"]


        # Check if the transaction's category list matches the start of any rent path
        if category_list and len(category_list) >= len(rent_category_path_1) and \
           all(c.lower() == rent_category_path_1[i] for i, c in enumerate(category_list[:len(rent_category_path_1)])):
            # logger.debug(f"Identified as Rent by category path 1: ID={transaction_id[:8]}...")
            return True

        if category_list and len(category_list) >= len(rent_category_path_2) and \
           all(c.lower() == rent_category_path_2[i] for i, c in enumerate(category_list[:len(rent_category_path_2)])):
             # logger.debug(f"Identified as Rent by category path 2: ID={transaction_id[:8]}...")
             return True


        # Option 2: Check simpler category terms (broader)
        # Check if 'rent' is *anywhere* in the flattened category string
        if 'rent' in transaction.get('category', '').lower():
            # logger.debug(f"Identified as Rent by category keyword: ID={transaction_id[:8]}...")
            return True

        # Option 3: Check transaction name (less reliable, use specific patterns)
        # Add your landlord's name or specific memo text patterns here
        # Use regex for more complex patterns if needed
        rent_name_patterns = ['rent payment', 'mthly rent']
        # Add your landlord's specific name(s) or common transaction names here
        landlord_names = ['your landlord name 1', 'your landlord name 2'] # <-- Add specific names/patterns here

        if any(pattern in name_lower for pattern in rent_name_patterns):
             # logger.debug(f"Identified as Rent by name pattern: ID={transaction_id[:8]}...")
             return True
        if any(landlord_name in name_lower for landlord_name in landlord_names):
             # logger.debug(f"Identified as Rent by landlord name: ID={transaction_id[:8]}...")
             return True


        # --- End Customization ---

        return False
    except Exception as e:
        logger.error(f"Error in is_rent_transaction for ID={transaction_id}: {str(e)}", exc_info=True) # Add exc_info
        return False  # Default to false on error

# --- Simple test route to verify application is running ---
@app.route('/test')
def test():
    # This route doesn't use the global logger, so no need for `global logger`
    return "Application is running correctly!"

# --- Test route for Plaid connectivity ---
@app.route('/plaid_test')
def plaid_test():
    global logger # Ensure logger is accessible in this function
    try:
        logger.info("Testing Plaid connection")

        # Check environment variables
        env_vars = {
            'PLAID_ENV': PLAID_ENV,
            'PLAID_CLIENT_ID': 'SET' if PLAID_CLIENT_ID else 'MISSING',
            'PLAID_SECRET': 'SET' if PLAID_SECRET else 'MISSING',
            'PLAID_ACCESS_TOKEN_PRIMARY': 'SET' if PLAID_ACCESS_TOKEN else 'MISSING'
        }

        result = f"<h1>Plaid Test Results</h1>"
        result += "<h2>Environment Variables</h2>"
        result += "<ul>"
        for key, value in env_vars.items():
            color = 'green' if value == 'SET' else ('orange' if key == 'PLAID_ACCESS_TOKEN_PRIMARY' else 'red')
            result += f"<li style='color:{color}'>{key}: {value}</li>"
        result += "</ul>"

        # Test client initialization
        client = initialize_plaid_client() # This function handles errors and flashes

        if not client:
            result += "<h2>Client Initialization</h2>"
            result += "<p style='color:red'>Failed to initialize Plaid client. Check logs for details and .env configuration.</p>"
            return result # Stop here if client couldn't initialize

        result += "<h2>Client Initialization</h2>"
        result += "<p style='color:green'>Successfully initialized Plaid client.</p>"

        # Test account access if access token is available
        if PLAID_ACCESS_TOKEN:
            try:
                accounts_request = AccountsGetRequest(access_token=PLAID_ACCESS_TOKEN)
                accounts_response = client.accounts_get(accounts_request)

                account_list = accounts_response.get('accounts', []) # Use .get
                account_count = len(account_list)
                result += "<h2>Accounts Access</h2>"
                if account_count > 0:
                    result += f"<p style='color:green'>Successfully fetched {account_count} accounts.</p>"

                    result += "<h3>Account Details (First 5)</h3>"
                    result += "<ul>"
                    # Display details for a few accounts
                    for i, account in enumerate(account_list):
                         if i >= 5: break
                         result += f"<li>{account.name} ({account.mask}) - Type: {account.type}, Subtype: {account.subtype}, Current Balance: ${account.balances.current if account.balances and account.balances.current is not None else 'N/A'}</li>"
                    if account_count > 5:
                         result += f"<li>... {account_count - 5} more accounts</li>"
                    result += "</ul>"
                else:
                     result += "<p style='color:orange'>Successfully connected, but no accounts found for this access token.</p>"


            except plaid.ApiException as e:
                try:
                    error_response = json.loads(e.body)
                    error_message = error_response.get('error_message', 'Unknown Plaid API error')
                    error_code = error_response.get('error_code', 'UNKNOWN')
                    result += "<h2>Accounts Access</h2>"
                    result += f"<p style='color:red'>Plaid API Error accessing accounts: {error_message} ({error_code})</p>"
                    logger.error(f"Plaid API Error in /plaid_test (Accounts): {error_code} - {error_message}", exc_info=True)
                    if error_code == 'ITEM_LOGIN_REQUIRED':
                        result += "<p style='color:orange'>ITEM_LOGIN_REQUIRED: Bank connection needs update. Re-link required.</p>"

                except (json.JSONDecodeError, KeyError):
                     result += "<h2>Accounts Access</h2>"
                     result += f"<p style='color:red'>Plaid API Error accessing accounts (could not parse response): {str(e)}</p>"
                     logger.error(f"Plaid API Error in /plaid_test (Accounts, unparseable): {e}", exc_info=True)

            except Exception as e:
                result += "<h2>Accounts Access</h2>"
                result += f"<p style='color:red'>An unexpected error occurred accessing accounts: {str(e)}</p>"
                logger.error(f"Error in /plaid_test (Accounts access): {str(e)}", exc_info=True)
        else:
            result += "<h2>Accounts Access</h2>"
            result += "<p style='color:orange'>No access token available (`PLAID_ACCESS_TOKEN_PRIMARY`) for testing account access.</p>"
            # flash("PLAID_ACCESS_TOKEN_PRIMARY not set. Cannot test account access.", "warning") # Avoid flashing on a test route

        # Add a link back to the main page
        result += "<p><a href='/'>Back to Expense Tracker</a></p>"

        return result
    except Exception as e:
        logger.error(f"Fatal error in plaid_test route: {str(e)}", exc_info=True) # Add exc_info
        traceback.print_exc()
        return f"""
        <h1>Test Error</h1>
        <p>A critical error occurred in the test route itself:</p>
        <pre>{str(e)}</pre>
        <p>Please check the application logs (`app.log`) for the full traceback and more details.</p>
         <p><a href='/'>Back to Expense Tracker</a></p>
        """, 500 # Return 500 status code


# --- Flask Route for Main Page ---
@app.route('/')
def index():
    """Renders the main page, fetching and displaying transactions."""
    try:
        # Explicitly declare logger as global to ensure it refers
        # to the module-level variable defined at the top.
        global logger # Ensure logger is accessible in this function

        logger.info("Route requested: / (index)")

        # Get user excluded IDs from session, ensure it's a list for session storage
        if 'user_excluded_ids' not in session or not isinstance(session['user_excluded_ids'], list):
            session['user_excluded_ids'] = []
        # Convert to a set for efficient lookup during processing
        user_excluded_ids_set = set(session['user_excluded_ids'])
        user_excluded_count = len(user_excluded_ids_set)


        # Determine Target Week Dates & MTD Dates
        today_date = datetime.date.today()
        target_week_start = get_target_week_dates() # This function already handles errors and logging
        target_week_end = target_week_start + datetime.timedelta(days=6)
        prev_week_start = target_week_start - datetime.timedelta(days=7)
        next_week_start = target_week_start + datetime.timedelta(days=7)
        current_week_start = today_date - datetime.timedelta(days=today_date.weekday())
        is_current_week = (target_week_start == current_week_start)

        month_start_date = today_date.replace(day=1)
        month_end_date = today_date # For MTD, end date is always today (up to today)

        # Initialize all summary and data variables BEFORE conditional logic
        all_mtd_transactions = [] # Full list fetched from Plaid for the month
        transactions_to_display = [] # Filtered list for the target week display
        total_income_mtd = 0.0
        total_expenses_mtd = 0.0
        balance_mtd = 0.0
        total_income_week = 0.0
        total_expenses_week = 0.0
        balance_week = 0.0
        rent_total_mtd = 0.0
        total_expenses_mtd_without_rent = 0.0 # Initialize here
        excluded_in_target_week_count = 0 # Initialize here

        auto_excluded_ids = set() # Initialize here
        combined_excluded_ids = set(user_excluded_ids_set) # Start combined exclusions with user exclusions

        logger.debug("Initializing Plaid client for index route")
        plaid_client = initialize_plaid_client() # This function handles errors and flashes messages

        # Only attempt to fetch and process Plaid data if client and token are available
        if plaid_client and PLAID_ACCESS_TOKEN:
            logger.info(f"Attempting to fetch ALL Plaid transactions from {month_start_date.strftime('%Y-%m-%d')} to {month_end_date.strftime('%Y-%m-%d')}")
            # Fetch full month data
            all_mtd_transactions = get_plaid_transactions(
                plaid_client, PLAID_ACCESS_TOKEN, month_start_date, month_end_date
            ) # This function handles errors and flashes messages

            if all_mtd_transactions is not None:
                logger.debug(f"Successfully fetched {len(all_mtd_transactions)} transactions for MTD period.")

                # --- Determine Exclusions (Auto and Combined) ---
                logger.debug(f"Finding offsetting transfers in {len(all_mtd_transactions)} transactions")
                auto_excluded_ids = find_and_exclude_offsetting_transfers(all_mtd_transactions) # This function handles errors and logging
                combined_excluded_ids.update(auto_excluded_ids) # Combine user and auto exclusions
                logger.info(f"Total auto-excluded IDs: {len(auto_excluded_ids)}. Total combined excluded IDs: {len(combined_excluded_ids)}. Total user excluded IDs: {user_excluded_count}")


                # --- Calculate MTD Summaries (Iterate over ALL MTD transactions) ---
                logger.debug("Calculating MTD financial summaries (excluding combined_excluded_ids)")
                for t in all_mtd_transactions:
                    is_excluded = (t['id'] in combined_excluded_ids)

                    # Skip excluded items for MTD financial calculations
                    if is_excluded:
                        continue

                    # Identify rent transactions (only need to do this once per transaction for MTD rent sum)
                    is_rent = is_rent_transaction(t)

                    # Plaid amount: > 0 is Expense, < 0 is Income
                    if t['amount'] > 0:  # Expense
                        current_expense = t['amount']
                        total_expenses_mtd += current_expense
                        # Add to rent total if it's a rent expense
                        if is_rent:
                            rent_total_mtd += current_expense

                    elif t['amount'] < 0:  # Income
                        current_income = abs(t['amount'])
                        total_income_mtd += current_income

                # Calculate balances AFTER summing totals for MTD
                balance_mtd = total_income_mtd - total_expenses_mtd
                # Calculate MTD expenses without rent AFTER total_expenses_mtd and rent_total_mtd are calculated
                total_expenses_mtd_without_rent = total_expenses_mtd - rent_total_mtd


                # --- Filter Transactions for Weekly Display ---
                # Create the list that will be passed to the template (ONLY transactions in target week)
                transactions_to_display = [
                    t for t in all_mtd_transactions
                    if target_week_start <= t['date'] <= target_week_end
                ]
                logger.debug(f"Filtered {len(transactions_to_display)} transactions for display in target week ({target_week_start.strftime('%Y-%m-%d')} to {target_week_end.strftime('%Y-%m-%d')}).")


                # --- Calculate Weekly Summaries & Count Weekly Exclusions (Iterate over WEEKLY DISPLAY transactions) ---
                logger.debug("Calculating Weekly financial summaries and counting weekly exclusions (based on transactions_to_display)")
                # Re-initialize weekly sums to ensure they are only based on the *displayed* week's non-excluded items
                total_income_week = 0.0
                total_expenses_week = 0.0
                excluded_in_target_week_count = 0 # Re-initialize count here before looping the weekly list

                for t in transactions_to_display: # <-- Loop over the filtered weekly list!
                    is_excluded = (t['id'] in combined_excluded_ids)

                    # Count exclusions visible IN THE DISPLAYED WEEK
                    if is_excluded:
                        excluded_in_target_week_count += 1
                        continue # Skip excluded items for WEEKLY financial calculations

                    # Plaid amount: > 0 is Expense, < 0 is Income
                    if t['amount'] > 0:  # Expense
                         total_expenses_week += t['amount']
                    elif t['amount'] < 0:  # Income
                         total_income_week += abs(t['amount'])

                # Calculate balances AFTER summing totals for Week
                balance_week = total_income_week - total_expenses_week


                # Sort the list that will be displayed by date, newest first
                transactions_to_display.sort(key=lambda x: x['date'], reverse=True)

                logger.debug(f"Financial summary calculated: Week Income=${total_income_week:.2f}, "
                             f"Week Expenses=${total_expenses_week:.2f}, Week Net=${balance_week:.2f}. "
                             f"Month Income=${total_income_mtd:.2f}, Month Expenses=${total_expenses_mtd:.2f}, "
                             f"Month Expenses (No Rent)=${total_expenses_mtd_without_rent:.2f}, "
                             f"Month Net=${balance_mtd:.2f}. Rent=${rent_total_mtd:.2f}")

            else:
                 logger.warning("get_plaid_transactions returned None. Summary calculations skipped.")
                 # Variables like total_income_mtd, total_expenses_mtd, etc. remain 0.0 as initialized.
                 # total_expenses_mtd_without_rent also remains 0.0.
                 # transactions_to_display remains empty.
                 # combined_excluded_ids remains just user exclusions (as auto_excluded_ids was empty set).
                 # excluded_in_target_week_count remains 0 as initialized.


        elif not PLAID_ACCESS_TOKEN:
            logger.error("PLAID_ACCESS_TOKEN_PRIMARY not found in .env file. Skipping Plaid data fetch.")
            flash("PLAID_ACCESS_TOKEN_PRIMARY not found in .env file.", "danger")
            # All totals remain 0.0, transactions_to_display remains empty, combined_excluded_ids is just user exclusions.
            # excluded_in_target_week_count remains 0.

        else: # This means plaid_client is None (initialization failed)
            # Error message already flashed by initialize_plaid_client
            logger.error("Plaid client initialization failed. Cannot fetch transactions. Skipping Plaid data fetch.")
            # All totals remain 0.0, transactions_to_display remains empty, combined_excluded_ids is just user exclusions.
            # excluded_in_target_week_count remains 0.


        # Now, all variables passed to the template are guaranteed to be initialized
        # The template rendering will succeed regardless of whether Plaid data was fetched.
        logger.debug(f"Rendering template with excluded_in_target_week_count={excluded_in_target_week_count}") # Added log here

        return render_template(
            'index.html',
            transactions=transactions_to_display, # This is the list for the TARGET WEEK
            # MTD Summary Data
            total_income_mtd=total_income_mtd,
            total_expenses_mtd=total_expenses_mtd,
            total_expenses_mtd_without_rent=total_expenses_mtd_without_rent,
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
            combined_excluded_ids=combined_excluded_ids, # Pass the combined set for marking rows
            user_excluded_ids=session['user_excluded_ids'], # Pass the list from session for button logic
            user_excluded_count=user_excluded_count, # Use the calculated count
            excluded_in_target_week_count=excluded_in_target_week_count # This variable is now initialized to 0 outside all conditionals
        )
    except Exception as e:
        # This logger call should now work
        logger.error(f"Fatal error in index route: {str(e)}", exc_info=True) # Add exc_info for full traceback in log file
        traceback.print_exc() # Still useful for immediate terminal output
        # Render a simple error page with debug links
        return f"""
        <h1>Application Error</h1>
        <p>A critical error occurred while processing your request:</p>
        <pre>{str(e)}</pre>
        <p>Please check the application logs (`app.log`) for the full traceback and more details.</p>
        <p><a href="/test">Click here to test if the Flask application is running</a></p>
        <p><a href="/plaid_test">Click here to test Plaid connectivity and credentials</a></p>
        """, 500 # Return 500 status code

# --- Other Routes (Refresh, Exclude, Clear) ---
# Add global logger to these functions too if they use logger
@app.route('/refresh', methods=['POST'])
def trigger_refresh():
    global logger # <--- Add global logger here
    try:
        logger.info("Route requested: /refresh (POST)")
        flash('Refreshing transaction data...', 'info')
        # Preserve the week_start parameter during redirect
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))
    except Exception as e:
        logger.error(f"Error in refresh route: {str(e)}", exc_info=True) # Add exc_info
        flash(f"Error refreshing data: {str(e)}", "danger")
        # Try to redirect back, preserving week_start
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))

@app.route('/exclude', methods=['POST'])
def exclude_transaction():
    global logger # <--- Add global logger here
    try:
        transaction_id = request.form.get('transaction_id')
        if transaction_id:
            if 'user_excluded_ids' not in session:
                session['user_excluded_ids'] = []
            current_user_excluded = session['user_excluded_ids']
            if transaction_id not in current_user_excluded:
                current_user_excluded.append(transaction_id)
                session['user_excluded_ids'] = current_user_excluded # Update session
                logger.info(f"User excluded transaction ID: {transaction_id}")
                flash(f'Transaction {transaction_id[:8]}... manually excluded.', 'warning')
            else:
                logger.debug(f"Attempted to exclude already excluded transaction: {transaction_id}")
                flash(f'Transaction {transaction_id[:8]}... was already manually excluded.', 'info')
        else:
            logger.warning("Exclude request received with missing transaction_id")
            flash('Could not exclude transaction: ID missing.', 'danger')
        # Preserve the week_start parameter during redirect
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))
    except Exception as e:
        logger.error(f"Error in exclude_transaction route: {str(e)}", exc_info=True) # Add exc_info
        flash(f"Error excluding transaction: {str(e)}", "danger")
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))


@app.route('/include', methods=['POST'])
def include_transaction():
    global logger # <--- Add global logger here
    try:
        transaction_id = request.form.get('transaction_id')
        if transaction_id:
            if 'user_excluded_ids' in session:
                current_user_excluded = session['user_excluded_ids']
                if transaction_id in current_user_excluded:
                    current_user_excluded.remove(transaction_id)
                    session['user_excluded_ids'] = current_user_excluded # Update session
                    logger.info(f"User included transaction ID: {transaction_id}")
                    flash(f'Transaction {transaction_id[:8]}... re-included.', 'info')
                else:
                    logger.debug(f"Attempted to include non-excluded transaction: {transaction_id}")
                    flash(f'Transaction {transaction_id[:8]}... was not manually excluded.', 'warning')
            else:
                logger.warning("Include request received but no 'user_excluded_ids' in session.")
                flash('No manual exclusions list found.', 'warning')
        else:
            logger.warning("Include request received with missing transaction_id")
            flash('Could not include transaction: ID missing.', 'danger')
        # Preserve the week_start parameter during redirect
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))
    except Exception as e:
        logger.error(f"Error in include_transaction route: {str(e)}", exc_info=True) # Add exc_info
        flash(f"Error including transaction: {str(e)}", "danger")
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))

@app.route('/clear_exclusions', methods=['POST'])
def clear_exclusions():
    global logger # <--- Add global logger here
    try:
        if 'user_excluded_ids' in session and session['user_excluded_ids']:
            session.pop('user_excluded_ids')
            logger.info("Cleared USER excluded transaction IDs.")
            flash('Manually excluded transactions reset.', 'info')
        else:
            logger.debug("Clear exclusions requested, but no manual exclusions found in session.")
            flash('No manual exclusions to clear.', 'info')
        # Preserve the week_start parameter during redirect
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))
    except Exception as e:
        logger.error(f"Error in clear_exclusions route: {str(e)}", exc_info=True) # Add exc_info
        flash(f"Error clearing exclusions: {str(e)}", "danger")
        week_start = request.args.get('week_start')
        return redirect(url_for('index', week_start=week_start))

# --- Error Handlers ---
@app.errorhandler(404)
def page_not_found(e):
    global logger # <--- Add global logger here
    logger.warning(f"404 Error: {request.url}") # Log the URL that was not found
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    global logger # <--- Add global logger here
    # Note: This handler might not catch errors that happen *before* the request context is fully set up,
    # like the NameError we saw initially. The traceback printed to the console/log is often better
    # for those early errors.
    logger.error(f"500 Internal Server Error: {request.url} - {str(e)}", exc_info=True) # Log the URL and error, add exc_info
    # The custom error page in index() might handle some 500s too.
    return render_template('500.html'), 500 # Render a generic 500 page

# --- Run the App ---
if __name__ == '__main__':
    # logger is available here globally
    if not app.secret_key or app.secret_key == "default-insecure-key-set-in-environment":
        logger.warning("\n*** WARNING: Flask Secret Key is not securely set! ***\n"
                       "*** Session data will not persist reliably between browser restarts. "
                       "Set FLASK_SECRET_KEY in your .env file. ***\n")
    logger.info("Starting Flask development server...")
    # Ensure debug=True is set for development
    app.run(debug=True, port=5001)