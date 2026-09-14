const SUPABASE_URL = "https://duihvlyfkjhairdzvdlm.supabase.co";

const SUPABASE_PUBLISHABLE_KEY = "KEEP_YOUR_EXISTING_PUBLISHABLE_KEY_HERE";

const supabaseClient = window.supabase.createClient(
  SUPABASE_URL,
  SUPABASE_PUBLISHABLE_KEY
);
