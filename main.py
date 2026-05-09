# これはサンプルの Python スクリプトです。

# Shift+F10 を押して実行するか、ご自身のコードに置き換えてください。
# Shift を2回押す を押すと、クラス/ファイル/ツールウィンドウ/アクション/設定を検索します。

from __future__ import annotations

import sys
import deadline
import deadline.client.api
from pathlib import Path
from botocore.exceptions import CredentialRetrievalError

from blendfile import read_blendfile_info


LOCAL_BLEND_PATH = Path(__file__).parent / "blender-4.5-splash.blend"
FRAMES_PER_TASK = 1  # OpenJD chunks.defaultTaskCount — how many frames each task renders


def print_hi(name):
    # スクリプトをデバッグするには以下のコード行でブレークポイントを使用してください。
    print(f'Hi, {name}')  # Ctrl+F8を押すとブレークポイントを切り替えます。


# ガター内の緑色のボタンを押すとスクリプトを実行します。
if __name__ == '__main__':
    print_hi('PyCharm')
    try:
        response = deadline.client.api.list_farms()
        print(response)
    except CredentialRetrievalError:
        print("認証が失敗しました。GUI から Login… ボタンで AWS SSO に")
        print("サインインしてから再度実行してください (`python gui.py`)。")
        sys.exit(1)

    blend_info = read_blendfile_info(LOCAL_BLEND_PATH)
    print(
        f"{LOCAL_BLEND_PATH.name}: Blender {blend_info.version} "
        f"(subversion {blend_info.subversion}), "
        f"frames {blend_info.start_frame}-{blend_info.end_frame}"
    )
    print(f"  renderer:    {blend_info.renderer}")
    print(f"  cameras:     {blend_info.cameras}")
    print(f"  view_layers: {blend_info.view_layers}")


'''
    job_bundle_dir = Path(__file__).parent

    response = deadline.client.api.create_job_from_job_bundle(
        job_bundle_dir=str(job_bundle_dir),
        job_parameters=[
            {"name": "SceneFile", "value": str(LOCAL_BLEND_PATH)},
            {"name": "Frames", "value": f"{blend_info.start_frame}-{blend_info.end_frame}"},
            {"name": "FramesPerTask", "value": str(FRAMES_PER_TASK)},
        ],
        print_function_callback=print,
        interactive_confirmation_callback=lambda message, default: True,
    )

    print("Submitted!")
    print(response)


    '''
# PyCharm のヘルプは https://www.jetbrains.com/help/pycharm/ を参照してください
