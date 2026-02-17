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
    
    # 檢查圖片是否已存在
    filename = os.path.basename(file_path)
    slug = os.path.splitext(filename)[0]
    try:
        check_response = requests.get(api_url, headers=headers, params={'slug': slug})
        if check_response.status_code == 200:
            existing_media = check_response.json()
            if existing_media:
                print(f"圖片已存在，跳過上傳: {filename} (ID: {existing_media[0]['id']})")
                return existing_media[0]
    except Exception as e:
        print(f"檢查圖片重複失敗: {e}")

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

def create_post(title, content, featured_media_id=None):
    """建立 WordPress 文章草稿"""
    url = os.environ.get('WP_URL')
    if not url:
        print("錯誤：請設定 WP_URL 環境變數。")
        sys.exit(1)

    api_url = f"{url}/wp-json/wp/v2/posts"
    headers = get_auth_header()
    
    post_data = {
        'title': title,
        'content': content,
        'status': 'draft'
    }
    
    if featured_media_id:
        post_data['featured_media'] = featured_media_id
        
    print(f"正在建立文章草稿: {title} ...")
    response = requests.post(api_url, headers=headers, json=post_data)
    response.raise_for_status()
    return response.json()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='發布文章到 WordPress')
    parser.add_argument('--title', required=True, help='文章標題')
    parser.add_argument('--content', required=True, help='文章 HTML 內容')
    parser.add_argument('--featured_image', help='精選圖片的本地路徑 (例如: ./photos/cover.jpg)')
    parser.add_argument('--image_dir', help='包含要上傳並插入文章的圖片目錄路徑')
    args = parser.parse_args()

    # 處理內容
    final_content = args.content
    featured_media_id = None

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
                try:
                    media_data = upload_media(img_path)
                    gallery_html += f'<!-- wp:image {{"id":{media_data["id"]},"sizeSlug":"large","linkDestination":"none"}} --><figure class="wp-block-image size-large"><img src="{media_data["source_url"]}" alt="" class="wp-image-{media_data["id"]}"/></figure><!-- /wp:image -->'
                except Exception as e:
                    print(f"上傳圖片失敗 {img_path}: {e}")
            gallery_html += '</figure><!-- /wp:gallery -->'
            final_content += gallery_html

    # 2. 處理精選圖片 (封面圖)
    if args.featured_image:
        try:
            media_data = upload_media(args.featured_image)
            featured_media_id = media_data['id']
            print(f"精選圖片上傳成功，ID: {featured_media_id}")
        except Exception as e:
            print(f"上傳精選圖片失敗: {e}")

    # 3. 建立文章草稿
    try:
        post = create_post(args.title, final_content, featured_media_id)
        print(f"\n✅ 文章草稿建立成功！")
        print(f"文章 ID: {post['id']}")
        print(f"預覽連結: {post['link']}")
        print(f"編輯連結: {os.environ.get('WP_URL')}/wp-admin/post.php?post={post['id']}&action=edit")
    except Exception as e:
        print(f"\n❌ 建立文章失敗: {e}")
        # 如果失敗，還是印出內容供手動貼上
        print("\n--- 備份：文章 HTML 內容 ---\n")
        print(final_content)