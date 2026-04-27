import time
from waveshare_epd import epd13in3k
from PIL import Image, ImageDraw

def deep_clean():
    try:
        epd = epd13in3k.EPD()
        epd.init()
        
        # Dimensions for your 13.3 inch screen
        width = 960
        height = 680
        
        print("Starting Monthly Deep Clean...")

        # 1. Solid Black Cycle
        print("Stage 1: Solid Black")
        black_img = Image.new('1', (width, height), 0) # 0 is black
        epd.display(epd.getbuffer(black_img))
        time.sleep(10) # Let the ink settle

        # 2. Solid White Cycle
        print("Stage 2: Solid White")
        white_img = Image.new('1', (width, height), 255) # 255 is white
        epd.display(epd.getbuffer(white_img))
        time.sleep(10)

        # 3. Final Refresh (Some prefer a second quick Black/White flick)
        print("Stage 3: Finalizing...")
        epd.init() # Re-init to clear any controller artifacts
        
        print("Deep Clean Complete. Powering down display.")
        epd.sleep()

    except Exception as e:
        print(f"Deep Clean Error: {e}")

if __name__ == "__main__":
    deep_clean()
