import { Link } from "react-router-dom";
import { Book } from "../api/client";

interface Props {
  book: Book;
  matchedTag?: string;
}

export default function BookCard({ book, matchedTag }: Props) {
  return (
    <Link
      to={`/books/${book.id}`}
      className="block bg-white rounded-xl shadow hover:shadow-lg transition-shadow overflow-hidden"
    >
      {/* 書影 */}
      <div className="bg-gray-100 h-48 flex items-center justify-center">
        {book.cover_image_url ? (
          <img
            src={book.cover_image_url}
            alt={book.title}
            className="h-full w-full object-cover"
          />
        ) : (
          <span className="text-gray-400 text-4xl">📚</span>
        )}
      </div>

      <div className="p-4">
        {/* タイトル・著者 */}
        <h3 className="font-bold text-gray-900 text-sm leading-tight line-clamp-2">
          {book.title}
        </h3>
        {book.author && (
          <p className="text-gray-500 text-xs mt-1">{book.author}</p>
        )}

        {/* 発売日 */}
        {book.published_date && (
          <p className="text-gray-400 text-xs mt-1">
            {new Date(book.published_date).toLocaleDateString("ja-JP")}
          </p>
        )}

        {/* マッチしたタグ */}
        {matchedTag && (
          <span className="inline-block mt-2 bg-indigo-100 text-indigo-700 text-xs px-2 py-0.5 rounded-full">
            ✓ {matchedTag}
          </span>
        )}

        {/* 雰囲気タグ */}
        <div className="flex flex-wrap gap-1 mt-2">
          {book.atmosphere_tags.slice(0, 3).map((tag) => (
            <span
              key={tag}
              className="bg-pink-50 text-pink-600 text-xs px-2 py-0.5 rounded-full"
            >
              {tag}
            </span>
          ))}
        </div>

        {/* AIサマリー */}
        {book.book_analyses?.gemini_summary && (
          <p className="text-gray-600 text-xs mt-2 line-clamp-2">
            {book.book_analyses.gemini_summary}
          </p>
        )}
      </div>
    </Link>
  );
}
