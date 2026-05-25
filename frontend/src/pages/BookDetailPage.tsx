import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchBook, Book } from "../api/client";

type BookDetail = Book & {
  reviews: { content: string; tweeted_at: string }[];
  book_analyses: {
    gemini_summary: string | null;
    atmosphere_keywords: string[];
    themes: string[];
  } | null;
};

export default function BookDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [book, setBook] = useState<BookDetail | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    fetchBook(id)
      .then((res) => setBook(res.data as BookDetail))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-gray-400">
        読み込み中...
      </div>
    );
  }

  if (!book) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center text-gray-400">
        <p>書籍が見つかりません</p>
        <Link to="/" className="mt-4 text-indigo-600 underline">
          トップに戻る
        </Link>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-indigo-700 text-white py-4 px-4">
        <div className="max-w-3xl mx-auto flex items-center gap-3">
          <Link to="/" className="text-indigo-200 hover:text-white text-sm">
            ← 戻る
          </Link>
          <span className="text-indigo-300">|</span>
          <span className="text-sm font-medium">書籍詳細</span>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-4 py-8">
        <div className="bg-white rounded-2xl shadow p-6 mb-6">
          <div className="flex gap-6">
            {/* 書影 */}
            <div className="flex-shrink-0 w-32 h-44 bg-gray-100 rounded-lg flex items-center justify-center overflow-hidden">
              {book.cover_image_url ? (
                <img
                  src={book.cover_image_url}
                  alt={book.title}
                  className="w-full h-full object-cover"
                />
              ) : (
                <span className="text-gray-400 text-4xl">📚</span>
              )}
            </div>

            {/* 書籍情報 */}
            <div className="flex-1">
              <h1 className="text-xl font-bold text-gray-900">{book.title}</h1>
              {book.author && (
                <p className="text-gray-600 mt-1">{book.author}</p>
              )}
              {book.published_date && (
                <p className="text-gray-400 text-sm mt-1">
                  発売日：{new Date(book.published_date).toLocaleDateString("ja-JP")}
                </p>
              )}
              <p className="text-xs text-gray-400 mt-1">{book.label}</p>

              {/* タグ */}
              <div className="flex flex-wrap gap-1 mt-3">
                {book.atmosphere_tags.map((tag) => (
                  <span
                    key={tag}
                    className="bg-pink-50 text-pink-600 text-xs px-2 py-0.5 rounded-full"
                  >
                    {tag}
                  </span>
                ))}
                {book.genre_tags.map((tag) => (
                  <span
                    key={tag}
                    className="bg-indigo-50 text-indigo-600 text-xs px-2 py-0.5 rounded-full"
                  >
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          </div>

          {/* あらすじ */}
          {book.description && (
            <div className="mt-6">
              <h2 className="text-sm font-bold text-gray-700 mb-2">📖 あらすじ</h2>
              <p className="text-gray-600 text-sm leading-relaxed">{book.description}</p>
            </div>
          )}
        </div>

        {/* AI分析サマリー */}
        {book.book_analyses?.gemini_summary && (
          <div className="bg-indigo-50 rounded-2xl p-6 mb-6">
            <h2 className="text-sm font-bold text-indigo-800 mb-2">
              🤖 AIによる読者感想まとめ
            </h2>
            <p className="text-indigo-700 text-sm leading-relaxed">
              {book.book_analyses.gemini_summary}
            </p>
            {book.book_analyses.atmosphere_keywords?.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-3">
                {book.book_analyses.atmosphere_keywords.map((kw) => (
                  <span
                    key={kw}
                    className="bg-indigo-100 text-indigo-600 text-xs px-2 py-0.5 rounded-full"
                  >
                    #{kw}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        {/* 感想ツイート */}
        {book.reviews?.length > 0 && (
          <div className="bg-white rounded-2xl shadow p-6">
            <h2 className="text-sm font-bold text-gray-700 mb-4">
              💬 読者の感想（Xより）
            </h2>
            <div className="space-y-3">
              {book.reviews.map((review, i) => (
                <div key={i} className="border-l-4 border-indigo-200 pl-3">
                  <p className="text-gray-600 text-sm leading-relaxed">
                    {review.content}
                  </p>
                  {review.tweeted_at && (
                    <p className="text-gray-400 text-xs mt-1">
                      {new Date(review.tweeted_at).toLocaleDateString("ja-JP")}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
