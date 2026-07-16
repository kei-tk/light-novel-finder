import axios from "axios";
import { supabase } from "../lib/supabase";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000/api";

export const apiClient = axios.create({
  baseURL: API_BASE,
  headers: { "Content-Type": "application/json" },
});

// 認証トークンを自動で付与
apiClient.interceptors.request.use(async (config) => {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// ---------- 型定義 ----------

export interface Series {
  id: string;
  series_title: string;
  author: string | null;
  illustrator: string | null;
  publisher: string | null;
  imprint: string | null;
  cover_image_url: string | null;
  description: string | null;
  synopsis: string | null;
  volume_count: number;
  is_series: boolean;
  parent_series_id: string | null;
  first_published_date: string | null;
  latest_published_date: string | null;
  atmosphere_tags: string[] | null;
  genre_tags: string[] | null;
  sub_genre_tags: string[] | null;
  comedy_score: number | null;
  serious_score: number | null;
  heartwarming_score: number | null;
  bittersweet_score: number | null;
  romantic_score: number | null;
  uplifting_score: number | null;
  popularity_score: number | null;
  momentum_score: number | null;
  // 6観点の説明テキスト (migration 030 / collect_aspect_embeddings)
  plot_text: string | null;
  cast_text: string | null;
  theme_text: string | null;
  mood_text: string | null;
  reader_text: string | null;
  review_aspect_text: string | null;
}

export interface Book {
  isbn: string;
  title: string;
  author: string | null;
  illustrator: string | null;
  volume_number: number | null;
  volume_number_sub: string | null;
  attached_to_isbn: string | null;
  published_date: string | null;
  cover_image_url: string | null;
  description: string | null;
}

export interface SubSeries {
  id: string;
  series_title: string;
  volume_count: number;
  cover_image_url: string | null;
  latest_published_date: string | null;
}

export interface SeriesDetail extends Series {
  books: Book[];
  sub_series: SubSeries[];
}

export interface Profile {
  id: string;
  display_name: string | null;
  bio: string | null;
  favorite_genres: string[] | null;
  favorite_atmospheres: string[] | null;
}

export interface SeriesStatus {
  is_favorite: boolean;
  is_read: boolean;
  rating: number | null;
}

// ---------- シリーズ系 ----------

export interface ListParams {
  // タグはカンマ区切りで複数指定可 (AND 絞り込み)
  genre?: string;
  atmosphere?: string;
  sub_genre?: string;
  author?: string;
  sort?: "popularity" | "momentum" | "new" | "title";
  min_comedy?: number;
  min_serious?: number;
  min_heartwarming?: number;
  min_bittersweet?: number;
  min_romantic?: number;
  min_uplifting?: number;
  limit?: number;
  offset?: number;
}

export const fetchSeriesList = (params?: ListParams) =>
  apiClient.get<{ series: Series[]; count: number; total: number }>("/series/", { params });

export const fetchSeriesByAuthor = (
  author: string,
  params?: { sort?: ListParams["sort"]; limit?: number; offset?: number }
) =>
  apiClient.get<{ series: Series[]; count: number; total: number }>("/series/", {
    params: { author, ...params },
  });

export const fetchNewReleases = (limit = 12) =>
  apiClient.get<{ series: Series[] }>("/series/new", { params: { limit } });

export const fetchTrending = (limit = 12) =>
  apiClient.get<{ series: Series[] }>("/series/trending", { params: { limit } });

export const fetchRecommended = (limit = 12) =>
  apiClient.get<{ series: Series[] }>("/series/recommended", { params: { limit } });

export const searchSeries = (q: string, params?: { limit?: number; offset?: number }) =>
  apiClient.get<{ series: Series[]; query: string; total: number }>("/series/search", {
    params: { q, ...params },
  });

export const fetchTags = () =>
  apiClient.get<{
    atmosphere_tags: string[];
    genre_tags: string[];
    sub_genre_tags: string[];
  }>("/series/tags");

export const fetchSeriesDetail = (id: string) =>
  apiClient.get<SeriesDetail>(`/series/${id}`);

// ---------- ベクトル検索 (類似 / 自然言語 / おすすめ) ----------

// 関連度しきい値以上をランク順に返す。offset/limit で「さらに見る」式に取得する。
export const semanticSearch = (
  q: string,
  opts?: { genres?: string[]; offset?: number; limit?: number; minRatio?: number }
) =>
  apiClient.get<{
    query: string;
    series: Series[];
    total: number;
    offset: number;
    has_more: boolean;
  }>("/series/semantic", {
    params: {
      q,
      ...(opts?.offset != null ? { offset: opts.offset } : {}),
      ...(opts?.limit != null ? { limit: opts.limit } : {}),
      ...(opts?.minRatio != null ? { min_ratio: opts.minRatio } : {}),
      ...(opts?.genres && opts.genres.length ? { genre: opts.genres.join(",") } : {}),
    },
  });

export const fetchSimilarSeries = (id: string, limit = 12) =>
  apiClient.get<{ series: Series[] }>(`/series/${id}/similar`, { params: { limit } });

export const fetchRecommendations = (limit = 12) =>
  apiClient.get<{ series: Series[]; based_on: number }>("/users/me/recommendations", {
    params: { limit },
  });

// ---------- ユーザー系 ----------

export const fetchProfile = () => apiClient.get<Profile>("/users/me/profile");

export const updateProfile = (body: Partial<Profile>) =>
  apiClient.put<{ profile: Profile }>("/users/me/profile", body);

export const fetchFavorites = () =>
  apiClient.get<{ series: Series[] }>("/users/me/favorites");

export const addFavorite = (seriesId: string) =>
  apiClient.post(`/users/me/favorites/${seriesId}`);

export const removeFavorite = (seriesId: string) =>
  apiClient.delete(`/users/me/favorites/${seriesId}`);

export const fetchReadList = () =>
  apiClient.get<{ series: (Series & { rating: number | null; finished_at: string })[] }>(
    "/users/me/read"
  );

export const markAsRead = (seriesId: string, rating?: number) =>
  apiClient.post(`/users/me/read/${seriesId}`, { rating: rating ?? null });

export const unmarkRead = (seriesId: string) =>
  apiClient.delete(`/users/me/read/${seriesId}`);

export const fetchSeriesStatus = (seriesId: string) =>
  apiClient.get<SeriesStatus>(`/users/me/status/${seriesId}`);
