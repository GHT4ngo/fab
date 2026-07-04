package com.fabscanner.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.view.View

/** Cyber HUD framing: dim full outline + bright cyan corner brackets, plus a cyan
 *  target strip over the footer code area. Matches the web app's cyan/dark theme. */
class CardGuideView(context: Context) : View(context) {
    private val cyan = Color.rgb(0, 240, 255)

    private val faintOutline = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(60, 0, 240, 255)
        style = Paint.Style.STROKE
        strokeWidth = 2f
    }
    private val corner = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = cyan
        style = Paint.Style.STROKE
        strokeWidth = 6f
        strokeCap = Paint.Cap.ROUND
    }
    private val footerFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(46, 0, 240, 255)
        style = Paint.Style.FILL
    }
    private val footerStroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(200, 0, 240, 255)
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

        // Faint full frame + rounded corner brackets.
        val r = 22f
        canvas.drawRoundRect(card, r, r, faintOutline)
        val len = minOf(w, h) * 0.12f
        // top-left
        canvas.drawLine(card.left, card.top + len, card.left, card.top + r, corner)
        canvas.drawLine(card.left + r, card.top, card.left + len, card.top, corner)
        // top-right
        canvas.drawLine(card.right - len, card.top, card.right - r, card.top, corner)
        canvas.drawLine(card.right, card.top + r, card.right, card.top + len, corner)
        // bottom-left
        canvas.drawLine(card.left, card.bottom - len, card.left, card.bottom - r, corner)
        canvas.drawLine(card.left + r, card.bottom, card.left + len, card.bottom, corner)
        // bottom-right
        canvas.drawLine(card.right - len, card.bottom, card.right - r, card.bottom, corner)
        canvas.drawLine(card.right, card.bottom - r, card.right, card.bottom - len, corner)

        // Footer code target strip.
        val footer = RectF(
            card.left,
            card.top + card.height() * 0.91f,
            card.right,
            card.top + card.height() * 0.98f,
        )
        canvas.drawRoundRect(footer, 8f, 8f, footerFill)
        canvas.drawRoundRect(footer, 8f, 8f, footerStroke)
    }
}
