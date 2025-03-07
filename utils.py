import json

def get_stored_tokens(database, selling_partner_id):
    """
    Fetch stored OAuth tokens from the database.
    :param database: The database session/connection object.
    :param selling_partner_id: The ID of the selling partner.
    :return: A dictionary with stored tokens or None if not found.
    """
    stored_value = database.get(selling_partner_id)  # Fetch tokens using the provided database

    if stored_value:
        if isinstance(stored_value, dict):
            return stored_value  # Already a dictionary

        try:
            return json.loads(stored_value)  # Convert JSON string to dictionary
        except json.JSONDecodeError:
            print(f"❌ Error: Stored tokens are not in JSON format! {stored_value}")
            return None

    return None

