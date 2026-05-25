import axios from "axios";

const API_BASE = import.meta.env.VITE_API_URL || "http://localhost:8000/api";

export const apiClient = axios.create({
  baseURL: API_BASE,
  headers: { "Content-Type": "application/json" },
});

export interface Book {
  id: string;
  title: string;
  author: string | null;
  published_date: string | null;
  description: string | null;
  cover_image_url: string | null;
  atmosphere_tags: string[];
  genre_tags: string[];
  label: string;
  book_analyses?: {
    gemini_summary: string | null;
    atmosphere_keywords: string[];
  } | null;
}

export interface BooksResponse {
  books: Book[];
  total: number;
}

export interface RecommendResponse {
  books: (Book & { matched_tag: string })[];
  matched_tags: string[];
}

export interface TagsResponse {
  atmosphere_tags: string[];
  genre_tags: string[];
}

// 書籍一覧取得
export const fetchBooks = (params?: {
  genre?: string;
  atmosphere?: string;
  limit?: number;
  offset?: number;
}) => apiClient.get<BooksResponse>("/books/", { params });

// 書籍詳細取得
export const fetchBook = (id: string) =>
  apiClient.get<Book & { reviews: { content: string; tweeted_at: string }[] }>(
    `/books/${id}`
  );

// タグ一覧取得
export const fetchTags = () => apiClient.get<TagsResponse>("/books/tags/list");

// 雰囲気ベース推薦
export const fetchRecommend = (preference: string, limit = 10) =>
  apiClient.post<RecommendResponse>("/recommend/", { preference, limit });
