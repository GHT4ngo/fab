package com.fabscanner.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.view.View

class CardGuideView(context: Context) : View(context) {
    private val cardPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(210, 34, 197, 94)
        style = Paint.Style.STROKE
        strokeWidth = 4f
    }
    private val footerPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(90, 34, 197, 94)
        style = Paint.Style.FILL
    }
    private val footerStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.rgb(34, 197, 94)
        style = Paint.Style.STROKE
        strokeWidth = 3f
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val cardAspect = 5f / 7f
        var h = height * 0.72f
        var w = h * cardAspect
        val maxW = width * 0.92f
        if (w > maxW) {
            w = maxW
            h = w / cardAspect
        }
        val left = (width - w) / 2f
        val top = (height - h) / 2f
        val card = RectF(left, top, left + w, top + h)
        canvas.drawRect(card, cardPaint)

        val footer = RectF(
            card.left,
            card.top + card.height() * 0.91f,
            card.right,
            card.top + card.height() * 0.98f,
        )
        canvas.drawRect(footer, footerPaint)
        canvas.drawRect(footer, footerStroke)
    }
}
