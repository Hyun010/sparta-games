import re
import os
import zipfile

from django.core.files.storage import FileSystemStorage
from django.http import FileResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.db.models import Avg, Q
from django.db.models.functions import Round

from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated  # 로그인 인증토큰
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.decorators import permission_classes

from .models import (
    Game,
    Like,
    View,
    Review,
    Screenshot,
    GameCategory,
)
from accounts.models import BotCnt
from .serializers import (
    GameListSerializer,
    GameDetailSerializer,
    ReviewSerializer,
    ScreenshotSerializer,
    CategorySerailizer,
)

from django.conf import settings
from openai import OpenAI
from django.utils import timezone


class GameListAPIView(APIView):
    """
    포스트일 때 로그인 인증을 위한 함수
    """

    def get_permissions(self):  # 로그인 인증토큰
        permissions = super().get_permissions()

        if self.request.method.lower() == 'post':  # 포스트할때만 로그인
            permissions.append(IsAuthenticated())

        return permissions

    """
    게임 목록 조회
    """

    def get(self, request):
        category_q = request.query_params.get('category-q')
        game_q = request.query_params.get('game-q')
        maker_q = request.query_params.get('maker-q')
        gm_q = request.query_params.get('gm-q')
        order = request.query_params.get('order')
        search = request.query_params.get('search')

        # 검색옵션 별 목록화
        if category_q:
            rows = Game.objects.filter(category__name__icontains=category_q).filter(
                is_visible=True, register_state=1)
        elif game_q:
            rows = Game.objects.filter(
                is_visible=True,
                register_state=1,
                title__icontains=game_q
            )
        elif maker_q:
            rows = Game.objects.filter(maker__username__icontains=maker_q).filter(
                is_visible=True, register_state=1)
        elif gm_q:
            rows = Game.objects.filter(
                Q(title__contains=gm_q) | Q(maker__username__icontains=gm_q)
            ).filter(is_visible=True, register_state=1)
        else:
            rows = Game.objects.filter(is_visible=True, register_state=1)

        #rows = rows.annotate(star=Round(Avg('reviews__star'), 1))

        # 추가 옵션 정렬
        if order == 'new':
            rows = rows.order_by('-created_at')
        elif order == 'view':
            rows = rows.order_by('-view_cnt')
        elif order == 'star':
            rows = rows.order_by('-star')
        else:
            rows = rows.order_by('-created_at')

        # search_pagination
        if search:
            paginator = PageNumberPagination()
            result = paginator.paginate_queryset(rows, request)
            serializer = GameListSerializer(result, many=True)
            return paginator.get_paginated_response(serializer.data)

        serializer = GameListSerializer(rows, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    """
    게임 등록
    """

    def post(self, request):
        # Game model에 우선 저장
        game = Game.objects.create(
            title=request.data.get('title'),
            thumbnail=request.FILES.get('thumbnail'),
            youtube_url=request.data.get('youtube_url'),
            maker=request.user, #FE 확인필요
            content=request.data.get('content'),
            gamefile=request.FILES.get('gamefile'),
            base_control=request.data.get('base_control'),
            release_note=request.data.get('release_note'),
            star=0,
            review_cnt=0,
        )

        # 카테고리 저장
        category_data = request.data.get('category')
        if category_data:
            for item in category_data.split(','):
                game.category.add(GameCategory.objects.get(name=item))

        # 이후 Screenshot model에 저장
        screenshots = list()
        for item in request.FILES.getlist("screenshots"):
            screenshot = Screenshot.objects.create(
                src=item,
                game=game
            )
            screenshots.append(screenshot.src.url)

        return Response({"message":"게임업로드 성공했습니다"},status=status.HTTP_200_OK)


class GameDetailAPIView(APIView):
    """
    포스트일 때 로그인 인증을 위한 함수
    """

    def get_permissions(self):  # 로그인 인증토큰
        permissions = super().get_permissions()

        if self.request.method.lower() == ('put' or 'delete'):  # 포스트할때만 로그인
            permissions.append(IsAuthenticated())

        return permissions

    def get_object(self, game_pk):
        return get_object_or_404(Game, pk=game_pk, is_visible=True)

    """
    게임 상세 조회
    """

    def get(self, request, game_pk):
        game = self.get_object(game_pk)

        serializer = GameDetailSerializer(game)
        # data에 serializer.data를 assignment
        # serializer.data의 리턴값인 ReturnDict는 불변객체이다
        data = serializer.data

        screenshots = Screenshot.objects.filter(game_id=game_pk)
        screenshot_serializer = ScreenshotSerializer(screenshots, many=True)

        categories = game.category.all()
        category_serializer = CategorySerailizer(categories, many=True)

        data["screenshot"] = screenshot_serializer.data
        data['category'] = category_serializer.data

        return Response(data, status=status.HTTP_200_OK)

    """
    게임 수정
    """

    def put(self, request, game_pk):
        game = self.get_object(game_pk)
        
        # 작성한 유저이거나 관리자일 경우 동작함
        if game.maker == request.user or request.user.is_staff == True:
            if request.FILES.get("gamefile"): # 게임파일을 교체할 경우 검수페이지로 이동
                game.register_state = 0
                game.gamefile = request.FILES.get("gamefile")
            game.title = request.data.get("title", game.title)
            game.thumbnail = request.FILES.get("thumbnail", '')
            game.youtube_url = request.data.get("youtube_url", game.youtube_url)
            game.content = request.data.get("content", game.content)
            game.base_control=request.data.get('base_control', game.base_control),
            game.release_note=request.data.get('release_note', game.release_note),
            game.save()

            category_data = request.data.get('category')
            if category_data is not None: # 태그가 바뀔 경우 기존 태그를 초기화, 신규 태그로 교체
                game.category.clear()
                categories = [GameCategory.objects.get_or_create(name=item.strip())[
                    0] for item in category_data.split(',') if item.strip()]
                game.category.set(categories)

            # 기존 데이터 삭제
            pre_screenshots_data = Screenshot.objects.filter(game=game)
            pre_screenshots_data.delete()

            # 받아온 스크린샷으로 교체
            if request.data.get('screenshots'): 
                for item in request.FILES.getlist("screenshots"):
                    game.screenshots.create(src=item)

            return Response({"messege": "수정이 완료됐습니다"}, status=status.HTTP_200_OK)
        else:
            return Response({"error": "작성자가 아닙니다"}, status=status.HTTP_400_BAD_REQUEST)

    """
    게임 삭제
    """

    def delete(self, request, game_pk):
        game = self.get_object(game_pk)

        # 작성한 유저이거나 관리자일 경우 동작함
        if game.maker == request.user or request.user.is_staff == True:
            game.is_visible = False
            game.save()
            return Response({"message": "삭제를 완료했습니다"}, status=status.HTTP_200_OK)
        else:
            return Response({"error": "작성자가 아닙니다"}, status=status.HTTP_400_BAD_REQUEST)


class GameLikeAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, game_pk):
        game = get_object_or_404(Game, pk=game_pk)
        if game.like.filter(pk=request.user.pk).exists(): 
            # 수정
            game.like.remove(request.user)
            return Response({'message': "즐겨찾기 취소"}, status=status.HTTP_200_OK)
        else:
            # 생성
            game.like.add(request.user)
            return Response({'message': "즐겨찾기"}, status=status.HTTP_200_OK)


# class GameStarAPIView(APIView):
#     permission_classes = [IsAuthenticated]

#     def post(self, request, game_pk):
#         star_list = [1,2,3,4,5]
#         star = int(request.data['star'])
#         if star not in star_list:
#             star = 5
#         game = get_object_or_404(Game, pk=game_pk)
#         if game.stars.filter(user=request.user).exists():
#             # 수정
#             game.stars.filter(user=request.user).update(
#                 star=star)
#         else:
#             # 생성
#             Star.objects.create(
#                 star=star,
#                 user=request.user,
#                 game=game,
#             )
#         star_values=[item['star'] for item in game.stars.values()]
#         average_star = round(sum(star_values) / len(star_values),1)
#         return Response({"res":"ok","avg_star":average_star}, status=status.HTTP_200_OK)


class ReviewAPIView(APIView):
    def get_permissions(self):  # 로그인 인증토큰
        permissions = super().get_permissions()

        if self.request.method.lower() == 'post':  # 포스트할때만 로그인
            permissions.append(IsAuthenticated())

        return permissions

    def get(self, request, game_pk):
        reviews = Review.objects.all().filter(game=game_pk)
        serializer = ReviewSerializer(reviews, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request, game_pk):
        game = get_object_or_404(Game, pk=game_pk)  # game 객체를 올바르게 설정
        #별점 계산
        game.star=game.star+((request.data.get('star')-game.star)/(game.review_cnt+1))
        game.review_cnt=game.review_cnt+1
        game.save()

        serializer = ReviewSerializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            serializer.save(author=request.user, game=game)  # 데이터베이스에 저장
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ReviewDetailAPIView(APIView):
    def get_permissions(self):  # 로그인 인증토큰
        permissions = super().get_permissions()

        if self.request.method.lower() == ('put' or 'delete'):  # 포스트할때만 로그인
            permissions.append(IsAuthenticated())

        return permissions

    def get(self, request, review_id):
        review = Review.objects.get(pk=review_id)
        serializer = ReviewSerializer(review)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def put(self, request, review_id):
        review = get_object_or_404(Review, pk=review_id)

        # 작성한 유저이거나 관리자일 경우 동작함
        if request.user == review.author or request.user.is_staff == True:
            game_pk=request.data.get('game_pk')
            game = get_object_or_404(Game, pk=game_pk)  # game 객체를 올바르게 설정
            game.star=game.star+((request.data.get('star')-request.data.get('pre_star'))/(game.review_cnt))
            game.save()
            serializer = ReviewSerializer(
                review, data=request.data, partial=True)
            if serializer.is_valid(raise_exception=True):
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response({"error": "작성자가 아닙니다"}, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, review_id):
        review = get_object_or_404(Review, pk=review_id)

        # 작성한 유저이거나 관리자일 경우 동작함
        if request.user == review.author or request.user.is_staff == True:
            game_pk=request.data.get('game_pk')
            game = get_object_or_404(Game, pk=game_pk)  # game 객체를 올바르게 설정
            if game.review_cnt >1:
                game.star=game.star+((game.star-review.star)/(game.review_cnt-1))
            else:
                game.star=0
            game.review_cnt=game.review_cnt-1
            game.save()
            review.is_visible = False
            review.save()
            return Response({"message": "삭제를 완료했습니다"}, status=status.HTTP_200_OK)
        else:
            return Response({"error": "작성자가 아닙니다"}, status=status.HTTP_400_BAD_REQUEST)


class CategoryAPIView(APIView):
    def get(self, request):
        categories = GameCategory.objects.all()
        serializer = CategorySerailizer(categories, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        if request.user.is_staff is False:
            return Response({"error": "권한이 없습니다"}, status=status.HTTP_400_BAD_REQUEST)
        serializer = CategorySerailizer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            serializer.save()
            return Response({"message": "태그를 추가했습니다"}, status=status.HTTP_200_OK)

    def delete(self, request):
        if request.user.is_staff is False:
            return Response({"error": "권한이 없습니다"}, status=status.HTTP_400_BAD_REQUEST)
        category = get_object_or_404(GameCategory, pk=request.data['pk'])
        category.delete()
        return Response({"message": "삭제를 완료했습니다"}, status=status.HTTP_200_OK)


@api_view(['POST'])
def game_register(request, game_pk):
    # game_pk에 해당하는 row 가져오기 (게시 중인 상태이면서 '등록 중' 상태)
    row = get_object_or_404(
        Game, pk=game_pk, is_visible=True, register_state=0)

    # gamefile 필드에 저장한 경로값을 'path' 변수에 저장
    path = row.gamefile.url

    # ~/<업로드시각>_<압축파일명>.zip 에서 '<업로드시각>_<압축파일명>' 추출
    game_folder = path.split('/')[-1].split('.')[0]

    # 게임 폴더 경로(압축을 풀 경로): './media/games/<업로드시각>_<압축파일명>'
    game_folder_path = f"./media/games/{game_folder}"

    # index.html 우선 압축 해제
    zipfile.ZipFile(f"./{path}").extract("index.html", game_folder_path)

    """
    index.html 내용 수정
    <link> 태그 href 값 수정 (line: 7, 8)
    var buildUrl 변수 값 수정 (line: 59)
    
    new_lines: 덮어쓸 내용 저장
    is_check_build: Build 키워드 찾은 후 True로 변경 (이후 라인에서 Build 찾는 것을 피하기 위함)
    """

    new_lines = str()
    is_check_build = False

    # 덮어쓸 내용 담기
    with open(f"{game_folder_path}/index.html", 'r') as f:
        for line in f.readlines():
            if line.find('link') > -1:
                cursor = line.find('TemplateData')
                new_lines += line[:cursor] + \
                    f'/media/games/{game_folder}/' + line[cursor:]
            elif line.find('buildUrl') > -1 and not is_check_build:
                is_check_build = True
                cursor = line.find('Build')
                new_lines += line[:cursor] + \
                    f'/media/games/{game_folder}/' + line[cursor:]
            else:
                new_lines += line
    # 추가할 JavaScript 코드 (iframe의 width, height 조절을 위해 추가함)
    additional_script = """
    <script>
      function sendSizeToParent() {
        var canvas = document.querySelector("#unity-canvas");
        var width = canvas.clientWidth;
        var height = canvas.clientHeight;
        window.parent.postMessage({ width: width, height: height }, '*');
      }

      window.addEventListener('resize', sendSizeToParent);
      window.addEventListener('load', sendSizeToParent);
    </script>
    """
    # CSS 스타일 추가 (body 태그와 unity-container에 overflow: hidden 추가)
    new_lines = new_lines.replace(
        '<body', '<body style="margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden;"')
    new_lines = new_lines.replace(
        '<div id="unity-container"', '<div id="unity-container" style="width: 100%; height: 100%; overflow: hidden;"')

    # </body> 태그 전에 추가할 스크립트 삽입
    body_close_category_index = new_lines.find('</body>')
    new_lines = new_lines[:body_close_category_index] + \
        additional_script + new_lines[body_close_category_index:]

    # 덮어쓰기
    with open(f'{game_folder_path}/index.html', 'w') as f:
        f.write(new_lines)

    # index.html 외 다른 파일들 압축 해제
    zipfile.ZipFile(f"./{path}").extractall(
        path=game_folder_path,
        members=[item for item in zipfile.ZipFile(
            f"./{path}").namelist() if item != "index.html"]
    )

    # 게임 폴더 경로를 저장하고, 등록 상태 1로 변경(등록 성공)
    row.gamepath = game_folder_path[1:]
    row.register_state = 1
    row.save()

    # 알맞은 HTTP Response 리턴
    # return Response({"message": f"등록을 성공했습니다. (게시물 id: {game_pk})"}, status=status.HTTP_200_OK)
    return redirect("games:admin_list")


@api_view(['POST'])
def game_register_deny(request, game_pk):
    row = get_object_or_404(
        Game, pk=game_pk, is_visible=True, register_state=0)
    row.register_state = 2
    row.save()
    return redirect("games:admin_list")


@api_view(['POST'])
def game_dzip(request, game_pk):
    row = get_object_or_404(
        Game, pk=game_pk, register_state=0, is_visible=True)
    zip_path = row.gamefile.url
    zip_folder_path = "./media/zips/"
    zip_name = os.path.basename(zip_path)

    # FileSystemStorage 인스턴스 생성
    # zip_folder_path에 대해 FILE_UPLOAD_PERMISSIONS = 0o644 권한 설정
    # 파일을 어디서 가져오는지를 정하는 것으로 보면 됨
    # 디폴트 값: '/media/' 에서 가져옴
    fs = FileSystemStorage(zip_folder_path)

    # FileResponse 인스턴스 생성
    # FILE_UPLOAD_PERMISSIONS 권한을 가진 상태로 zip_folder_path 경로 내의 zip_name 파일에 'rb' 모드로 접근
    # content_type 으로 zip 파일임을 명시
    response = FileResponse(fs.open(zip_name, 'rb'),
                            content_type='application/zip')

    # 'Content-Disposition' value 값(HTTP Response 헤더값)을 설정
    # 파일 이름을 zip_name 으로 다운로드 폴더에 받겠다는 뜻
    response['Content-Disposition'] = f'attachment; filename="{zip_name}"'

    # FileResponse 객체를 리턴
    return response


CLIENT = OpenAI(api_key=settings.OPEN_API_KEY)
MAX_USES_PER_DAY = 10 # 하루 당 질문 10개로 제한기준

# chatbot API


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def ChatbotAPIView(request):
    user = request.user
    today = timezone.now().date()

    usage, created = BotCnt.objects.get_or_create(user=user, date=today)

    if usage.count >= MAX_USES_PER_DAY:
        return Response({"error": "Daily usage limit reached"}, status=status.HTTP_400_BAD_REQUEST)

    usage.count += 1
    usage.save()

    input_data = request.data.get('input_data')
    categorylist = list(GameCategory.objects.values_list('name', flat=True))
    
    # GPT API와 통신을 통해 답장을 받아온다.(아래 형식을 따라야함)(추가 옵션은 문서를 참고)
    instructions = f"""
    내가 제한한 카테고리 목록 : {categorylist} 여기서만 이야기를 해줘, 이외에는 말하지마
    받은 내용을 요약해서 내가 제한한 목록에서 제일 관련 있는 항목 한 개를 골라줘
    결과 형식은 다른 말은 없이 꾸미지도 말고 딱! '카테고리:'라는 형식으로만 작성해줘
    결과에 특수문자, 이모티콘 붙이지마
    """
    completion = CLIENT.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"받은 내용: {input_data}"},
        ],
    )

    # 응답받은 데이터 처리
    gpt_response = completion.choices[0].message.content
    about_category = gpt_response.split('태그:')[1]
    about_category = re.sub(
        '[-=+,#/\?:^.@*\"※~ㆍ!』‘|\(\)\[\]`\'…》\”\“\’·]', '', about_category)
    about_category = about_category.strip()
    uncategorylist = ['없음', '']
    if about_category in uncategorylist:
        about_category = '없음'
    return Response({"category": about_category}, status=status.HTTP_200_OK)


# ---------- Web ---------- #


# # 게임 등록 Api 테스트용 페이지 렌더링
# def game_detail_view(request, game_pk):
#     return render(request, "games/game_detail.html", {'game_pk': game_pk})


# # 테스트용 base.html 렌더링
# def test_base_view(request):
#     return render(request, "base.html")


# # 테스트용 메인 페이지 렌더링
# def main_view(request):
#     return render(request, "games/main.html")


# # 테스트용 검색 페이지 렌더링
# def search_view(request):
#     # 쿼리스트링을 그대로 가져다가 '게임 목록 api' 호출
#     return render(request, "games/search.html")


# # 게임 검수용 페이지 뷰
# def admin_list(request):
#     rows = Game.objects.filter(is_visible=True, register_state=0)
#     return render(request, "games/admin_list.html", context={"rows": rows})


# def admin_category(request):
#     categories = GameCategory.objects.all()
#     return render(request, "games/admin_tags.html", context={"categories": categories})


# def game_create_view(request):
#     return render(request, "games/game_create.html")


# def game_update_view(request, game_pk):
#     return render(request, "games/game_update.html", {'game_pk': game_pk})


# def chatbot_view(request):
#     return render(request, "games/chatbot.html")
