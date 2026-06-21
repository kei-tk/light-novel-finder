import { createClient } from "@supabase/supabase-js";

const url = import.meta.env.VITE_SUPABASE_URL as string;
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string;

if (!url || !anonKey) {
  // 開発初期で .env が未設定でもアプリが落ちないようにコンソールで警告
  console.warn(
    "[supabase] VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY が未設定。frontend/.env を確認してください。"
  );
}

export const supabase = createClient(url ?? "", anonKey ?? "");
