import os
import requests
import base64
import argparse
import sys
import mimetypes
import glob
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def get_auth_header():
    """取得驗證標頭"""
    user = os.environ.get('WP_USER')
    password = os.environ.get('WP_APP_PASSWORD')
    
    if not user or not password:
        print("錯誤：請設定 WP_USER 和 WP_APP_PASSWORD 環境變數。")
        sys.exit(1)
        
    credentials = f"{user}:{password}"
    token = base64.b64encode(credentials.encode())
    return {
        'Authorization': f'Basic {token.decode("utf-8")}'
    }

def upload_media(file_path):
    """上傳圖片並回傳 Media 物件 (包含 ID 和 URL)"""
    url = os.environ.get('WP_URL')
    if not url:
        print("錯誤：請設定 WP_URL 環境變數。")
        sys.exit(1)

    api_url = f"{url}/wp-json/wp/v2/media"
    headers = get_auth_header()
    
    # 準備檔案與標頭
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        mime_type = 'application/octet-stream'
        
    headers['Content-Disposition'] = f'attachment; filename={os.path.basename(file_path)}'
    headers['Content-Type'] = mime_type

    with open(file_path, 'rb') as img_file:
        print(f"DEBUG: Attempting to connect to: {api_url}")
        response = requests.post(api_url, headers=headers, data=img_file)
        response.raise_for_status()
        json_response = response.json()
        if isinstance(json_response, list):
            media_item = json_response[0]
        else:
            media_item = json_response
        
        print(f"圖片上傳成功！ID: {media_item['id']}")
        return media_item


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='發布文章到 WordPress')
    parser.add_argument('--title', required=True, help='文章標題')
    parser.add_argument('--content', required=True, help='文章 HTML 內容')
    parser.add_argument('--featured_image', help='精選圖片的本地路徑 (例如: ./photos/cover.jpg)')
    parser.add_argument('--image_dir', help='包含要上傳並插入文章的圖片目錄路徑')
    args = parser.parse_args()

    # 處理內容
    final_content = args.content

    # 1. 處理圖片目錄 (批次上傳並建立畫廊)
    if args.image_dir:
        print(f"正在掃描目錄: {args.image_dir} ...")
        image_files = []
        for ext in ['*.jpg', '*.jpeg', '*.png', '*.gif', '*.JPG', '*.JPEG', '*.PNG']:
            image_files.extend(glob.glob(os.path.join(args.image_dir, ext)))
        
        if image_files:
            print(f"發現 {len(image_files)} 張圖片，準備上傳並建立畫廊...")
            gallery_html = '<!-- wp:gallery {"linkTo":"none"} --><figure class="wp-block-gallery has-nested-images columns-default is-cropped">'
            for img_path in sorted(image_files):
                media_data = upload_media(img_path)
                gallery_html += f'<!-- wp:image {{"id":{media_data["id"]},"sizeSlug":"large","linkDestination":"none"}} --><figure class="wp-block-image size-large"><img src="{media_data["source_url"]}" alt="" class="wp-image-{media_data["id"]}"/></figure><!-- /wp:image -->'
            gallery_html += '</figure><!-- /wp:gallery -->'
            final_content += gallery_html

    # 2. 處理精選圖片 (封面圖)
    # The publish_post function has been removed. User will manually add featured image.
    if args.featured_image:
        print(f"請注意：已上傳精選圖片 '{args.featured_image}'，其 ID 為 {upload_media(args.featured_image)['id']}。")
        print("請在 WordPress 編輯器中手動設定此圖片為文章的精選圖片。")

    print("\n--- 請複製以下內容到您的 WordPress 網站後台 (文字模式或自訂 HTML 區塊) ---\n")
    print(f"文章標題: {args.title}")
    print("\n--- 文章內容 ---\n")
    print(final_content)
    print("\n--------------------------------------------------------------------------\n")
    print("請將上述「文章標題」貼至 WordPress 編輯器的標題欄位。")
    print("將「文章內容」完整貼至 WordPress 編輯器的「文字模式」或「自訂 HTML 區塊」。")
    print("圖片已上傳至您的媒體庫，您可以在編輯器中手動插入或設定特色圖片。")
    print("發布前請務必在 WordPress 後台預覽文章，確認排版無誤後再發布。")