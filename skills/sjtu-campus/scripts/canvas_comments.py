from __future__ import annotations

import argparse

from common import emit, request_json, run
from canvas import BASE_URL, token


def main() -> None:
    parser = argparse.ArgumentParser(description="Read Canvas submission comments")
    parser.add_argument("--course", type=int, required=True)
    parser.add_argument("--assignment", type=int, required=True)
    args = parser.parse_args()

    payload, _ = request_json(
        f"{BASE_URL}/api/v1/courses/{args.course}/assignments/{args.assignment}"
        "/submissions/self?include[]=submission_comments&include[]=attachments",
        token(),
    )
    comments = []
    for row in payload.get("submission_comments") or []:
        comments.append(
            {
                "id": row.get("id"),
                "authorId": row.get("author_id"),
                "authorName": row.get("author_name"),
                "createdAt": row.get("created_at"),
                "editedAt": row.get("edited_at"),
                "comment": row.get("comment"),
                "attachments": [
                    {
                        "id": item.get("id"),
                        "name": item.get("display_name"),
                        "contentType": item.get("content-type"),
                        "size": item.get("size"),
                    }
                    for item in row.get("attachments") or []
                ],
            }
        )
    comments.sort(key=lambda row: row["createdAt"] or "")
    emit(
        {
            "ok": True,
            "courseId": args.course,
            "assignmentId": args.assignment,
            "submission": {
                "state": payload.get("workflow_state"),
                "submittedAt": payload.get("submitted_at"),
                "gradedAt": payload.get("graded_at"),
                "score": payload.get("score"),
                "grade": payload.get("grade"),
                "attempt": payload.get("attempt"),
            },
            "count": len(comments),
            "comments": comments,
        }
    )


if __name__ == "__main__":
    run(main)
